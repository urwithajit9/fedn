import base64
import io
import json
import os
import queue
import re
import ssl
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from distutils.dir_util import copy_tree
from io import BytesIO

import grpc
from google.protobuf.json_format import MessageToJson

import fedn.common.net.grpc.fedn_pb2 as fedn
import fedn.common.net.grpc.fedn_pb2_grpc as rpc
from fedn.network.clients.connect import ConnectorClient, Status
from fedn.network.clients.package import PackageRuntime
from fedn.network.clients.state import ClientState, ClientStateToString
from fedn.utils.dispatcher import Dispatcher
from fedn.utils.helpers import get_helper
from fedn.utils.logger import Logger

CHUNK_SIZE = 1024 * 1024
VALID_NAME_REGEX = '^[a-zA-Z0-9_-]*$'


class GrpcAuth(grpc.AuthMetadataPlugin):
    def __init__(self, key):
        self._key = key

    def __call__(self, context, callback):
        callback((('authorization', f'Token {self._key}'),), None)


class Client:
    """FEDn Client. Service running on client/datanodes in a federation,
    recieving and handling model update and model validation requests.

    :param config: A configuration dictionary containing connection information for the discovery service (controller)
        and settings governing e.g. client-combiner assignment behavior.
    :type config: dict
    """

    def __init__(self, config):
        """Initialize the client."""

        self.state = None
        self.error_state = False
        self._attached = False
        self._missed_heartbeat = 0
        self.config = config

        self.connector = ConnectorClient(host=config['discover_host'],
                                         port=config['discover_port'],
                                         token=config['token'],
                                         name=config['name'],
                                         remote_package=config['remote_compute_context'],
                                         force_ssl=config['force_ssl'],
                                         verify=config['verify'],
                                         combiner=config['preferred_combiner'],
                                         id=config['client_id'])

        # Validate client name
        match = re.search(VALID_NAME_REGEX, config['name'])
        if not match:
            raise ValueError('Unallowed character in client name. Allowed characters: a-z, A-Z, 0-9, _, -.')

        self.name = config['name']
        dirname = time.strftime("%Y%m%d-%H%M%S")
        self.run_path = os.path.join(os.getcwd(), dirname)
        os.mkdir(self.run_path)

        self.logger = Logger(
            to_file=config['logfile'], file_path=self.run_path)
        self.started_at = datetime.now()
        self.logs = []

        self.inbox = queue.Queue()

        # Attach to the FEDn network (get combiner)
        client_config = self._attach()

        self._initialize_dispatcher(config)

        self._initialize_helper(client_config)
        if not self.helper:
            print("Failed to retrive helper class settings! {}".format(
                client_config), flush=True)

        self._subscribe_to_combiner(config)

        self.state = ClientState.idle

    def _assign(self):
        """Contacts the controller and asks for combiner assignment.

        :return: A configuration dictionary containing connection information for combiner.
        :rtype: dict
        """

        print("Asking for assignment!", flush=True)
        while True:
            status, response = self.connector.assign()
            if status == Status.TryAgain:
                print(response, flush=True)
                time.sleep(5)
                continue
            if status == Status.Assigned:
                client_config = response
                break
            if status == Status.UnAuthorized:
                print(response, flush=True)
                sys.exit("Exiting: Unauthorized")
            if status == Status.UnMatchedConfig:
                print(response, flush=True)
                sys.exit("Exiting: UnMatchedConfig")
            time.sleep(5)
            print(".", end=' ', flush=True)

        print("Got assigned!", flush=True)
        print("Received combiner config: {}".format(client_config), flush=True)
        return client_config

    def _connect(self, client_config):
        """Connect to assigned combiner.

        :param client_config: A configuration dictionary containing connection information for
        the combiner.
        :type client_config: dict
        """

        # TODO use the client_config['certificate'] for setting up secure comms'
        host = client_config['host']
        port = client_config['port']
        secure = False
        if client_config['fqdn'] is not None:
            host = client_config['fqdn']
            # assuming https if fqdn is used
            port = 443
        print(f"CLIENT: Connecting to combiner host: {host}:{port}", flush=True)

        if client_config['certificate']:
            print("CLIENT: using certificate from Reducer for GRPC channel")
            secure = True
            cert = base64.b64decode(
                client_config['certificate'])  # .decode('utf-8')
            credentials = grpc.ssl_channel_credentials(root_certificates=cert)
            channel = grpc.secure_channel("{}:{}".format(host, str(port)), credentials)
        elif os.getenv("FEDN_GRPC_ROOT_CERT_PATH"):
            secure = True
            print("CLIENT: using root certificate from environment variable for GRPC channel")
            with open(os.environ["FEDN_GRPC_ROOT_CERT_PATH"], 'rb') as f:
                credentials = grpc.ssl_channel_credentials(f.read())
            channel = grpc.secure_channel("{}:{}".format(host, str(port)), credentials)
        elif self.config['secure']:
            secure = True
            print("CLIENT: using CA certificate for GRPC channel")
            cert = ssl.get_server_certificate((host, port))

            credentials = grpc.ssl_channel_credentials(cert.encode('utf-8'))
            if self.config['token']:
                token = self.config['token']
                auth_creds = grpc.metadata_call_credentials(GrpcAuth(token))
                channel = grpc.secure_channel("{}:{}".format(host, str(port)), grpc.composite_channel_credentials(credentials, auth_creds))
            else:
                channel = grpc.secure_channel("{}:{}".format(host, str(port)), credentials)
        else:
            print("CLIENT: using insecure GRPC channel")
            if port == 443:
                port = 80
            channel = grpc.insecure_channel("{}:{}".format(
                host,
                str(port)))

        self.channel = channel

        self.connectorStub = rpc.ConnectorStub(channel)
        self.combinerStub = rpc.CombinerStub(channel)
        self.modelStub = rpc.ModelServiceStub(channel)

        print("Client: {} connected {} to {}:{}".format(self.name,
                                                        "SECURED" if secure else "INSECURE",
                                                        host,
                                                        port),
              flush=True)

        print("Client: Using {} compute package.".format(
            client_config["package"]))

    def _disconnect(self):
        """Disconnect from the combiner."""
        self.channel.close()

    def _detach(self):
        """Detach from the FEDn network (disconnect from combiner)"""
        # Setting _attached to False will make all processing threads return
        if not self._attached:
            print("Client is not attached.", flush=True)

        self._attached = False
        # Close gRPC connection to combiner
        self._disconnect()

    def _attach(self):
        """Attach to the FEDn network (connect to combiner)"""
        # Ask controller for a combiner and connect to that combiner.
        if self._attached:
            print("Client is already attached. ", flush=True)
            return None

        client_config = self._assign()
        self._connect(client_config)

        if client_config:
            self._attached = True
        return client_config

    def _initialize_helper(self, client_config):
        """Initialize the helper class for the client.

        :param client_config: A configuration dictionary containing connection information for
        | the discovery service (controller) and settings governing e.g.
        | client-combiner assignment behavior.
        :type client_config: dict
        :return:
        """

        if 'helper_type' in client_config.keys():
            self.helper = get_helper(client_config['helper_type'])

    def _subscribe_to_combiner(self, config):
        """Listen to combiner message stream and start all processing threads.

        :param config: A configuration dictionary containing connection information for
        | the discovery service (controller) and settings governing e.g.
        | client-combiner assignment behavior.
        """

        # Start sending heartbeats to the combiner.
        threading.Thread(target=self._send_heartbeat, kwargs={
                         'update_frequency': config['heartbeat_interval']}, daemon=True).start()

        # Start listening for combiner training and validation messages
        if config['trainer']:
            threading.Thread(
                target=self._listen_to_model_update_request_stream, daemon=True).start()
        if config['validator']:
            threading.Thread(
                target=self._listen_to_model_validation_request_stream, daemon=True).start()
        self._attached = True

        # Start processing the client message inbox
        threading.Thread(target=self.process_request, daemon=True).start()

    def _initialize_dispatcher(self, config):
        """ Initialize the dispatcher for the client.

        :param config: A configuration dictionary containing connection information for
        | the discovery service (controller) and settings governing e.g.
        | client-combiner assignment behavior.
        :type config: dict
        :return:
        """
        if config['remote_compute_context']:
            pr = PackageRuntime(os.getcwd(), os.getcwd())

            retval = None
            tries = 10

            while tries > 0:
                retval = pr.download(
                    host=config['discover_host'],
                    port=config['discover_port'],
                    token=config['token'],
                    force_ssl=config['force_ssl'],
                    secure=config['secure']
                )
                if retval:
                    break
                time.sleep(60)
                print("No compute package available... retrying in 60s Trying {} more times.".format(
                    tries), flush=True)
                tries -= 1

            if retval:
                if 'checksum' not in config:
                    print(
                        "\nWARNING: Skipping security validation of local package!, make sure you trust the package source.\n",
                        flush=True)
                else:
                    checks_out = pr.validate(config['checksum'])
                    if not checks_out:
                        print("Validation was enforced and invalid, client closing!")
                        self.error_state = True
                        return

            if retval:
                pr.unpack()

            self.dispatcher = pr.dispatcher(self.run_path)
            try:
                print("Running Dispatcher for entrypoint: startup", flush=True)
                self.dispatcher.run_cmd("startup")
            except KeyError:
                pass
        else:
            # TODO: Deprecate
            dispatch_config = {'entry_points':
                               {'predict': {'command': 'python3 predict.py'},
                                'train': {'command': 'python3 train.py'},
                                'validate': {'command': 'python3 validate.py'}}}
            from_path = os.path.join(os.getcwd(), 'client')

            copy_tree(from_path, self.run_path)
            self.dispatcher = Dispatcher(dispatch_config, self.run_path)

    def get_model(self, id):
        """Fetch a model from the assigned combiner.
        Downloads the model update object via a gRPC streaming channel.

        :param id: The id of the model update object.
        :type id: str
        :return: The model update object.
        :rtype: BytesIO
        """
        data = BytesIO()

        for part in self.modelStub.Download(fedn.ModelRequest(id=id)):

            if part.status == fedn.ModelStatus.IN_PROGRESS:
                data.write(part.data)

            if part.status == fedn.ModelStatus.OK:
                return data

            if part.status == fedn.ModelStatus.FAILED:
                return None

        return data

    def set_model(self, model, id):
        """Send a model update to the assigned combiner.
        Uploads the model updated object via a gRPC streaming channel, Upload.

        :param model: The model update object.
        :type model: BytesIO
        :param id: The id of the model update object.
        :type id: str
        :return: The model update object.
        :rtype: BytesIO
        """
        if not isinstance(model, BytesIO):
            bt = BytesIO()

            for d in model.stream(32 * 1024):
                bt.write(d)
        else:
            bt = model

        bt.seek(0, 0)

        def upload_request_generator(mdl):
            """Generator function for model upload requests.

            :param mdl: The model update object.
            :type mdl: BytesIO
            :return: A model update request.
            :rtype: fedn.ModelRequest
            """
            while True:
                b = mdl.read(CHUNK_SIZE)
                if b:
                    result = fedn.ModelRequest(
                        data=b, id=id, status=fedn.ModelStatus.IN_PROGRESS)
                else:
                    result = fedn.ModelRequest(
                        id=id, status=fedn.ModelStatus.OK)

                yield result
                if not b:
                    break

        result = self.modelStub.Upload(upload_request_generator(bt))

        return result

    def _listen_to_model_update_request_stream(self):
        """Subscribe to the model update request stream.

        :return: None
        :rtype: None
        """

        r = fedn.ClientAvailableMessage()
        r.sender.name = self.name
        r.sender.role = fedn.WORKER
        metadata = [('client', r.sender.name)]

        while True:
            try:
                for request in self.combinerStub.ModelUpdateRequestStream(r, metadata=metadata):
                    if request.sender.role == fedn.COMBINER:
                        # Process training request
                        self._send_status("Received model update request.", log_level=fedn.Status.AUDIT,
                                          type=fedn.StatusType.MODEL_UPDATE_REQUEST, request=request)

                        self.inbox.put(('train', request))

                    if not self._attached:
                        return
            except grpc.RpcError as e:
                _ = e.code()
            except grpc.RpcError:
                # TODO: make configurable
                timeout = 5
                time.sleep(timeout)
            except Exception:
                raise

            if not self._attached:
                return

    def _listen_to_model_validation_request_stream(self):
        """Subscribe to the model validation request stream.

        :return: None
        :rtype: None
        """

        r = fedn.ClientAvailableMessage()
        r.sender.name = self.name
        r.sender.role = fedn.WORKER
        while True:
            try:
                for request in self.combinerStub.ModelValidationRequestStream(r):
                    # Process validation request
                    _ = request.model_id
                    self._send_status("Recieved model validation request.", log_level=fedn.Status.AUDIT,
                                      type=fedn.StatusType.MODEL_VALIDATION_REQUEST, request=request)
                    self.inbox.put(('validate', request))

            except grpc.RpcError:
                # TODO: make configurable
                timeout = 5
                time.sleep(timeout)
            except Exception:
                raise

            if not self._attached:
                return

    def _process_training_request(self, model_id):
        """Process a training (model update) request.

        :param model_id: The model id of the model to be updated.
        :type model_id: str
        :return: The model id of the updated model, or None if the update failed. And a dict with metadata.
        :rtype: tuple
        """

        self._send_status(
            "\t Starting processing of training request for model_id {}".format(model_id))
        self.state = ClientState.training

        try:
            meta = {}
            tic = time.time()
            mdl = self.get_model(str(model_id))
            meta['fetch_model'] = time.time() - tic

            inpath = self.helper.get_tmp_path()
            with open(inpath, 'wb') as fh:
                fh.write(mdl.getbuffer())

            outpath = self.helper.get_tmp_path()
            tic = time.time()
            # TODO: Check return status, fail gracefully
            self.dispatcher.run_cmd("train {} {}".format(inpath, outpath))
            meta['exec_training'] = time.time() - tic

            tic = time.time()
            out_model = None
            with open(outpath, "rb") as fr:
                out_model = io.BytesIO(fr.read())

            # Push model update to combiner server
            updated_model_id = uuid.uuid4()
            self.set_model(out_model, str(updated_model_id))
            meta['upload_model'] = time.time() - tic

            # Read the metadata file
            with open(outpath+'-metadata', 'r') as fh:
                training_metadata = json.loads(fh.read())
            meta['training_metadata'] = training_metadata

            os.unlink(inpath)
            os.unlink(outpath)
            os.unlink(outpath+'-metadata')

        except Exception as e:
            print("ERROR could not process training request due to error: {}".format(
                e), flush=True)
            updated_model_id = None
            meta = {'status': 'failed', 'error': str(e)}

        self.state = ClientState.idle

        return updated_model_id, meta

    def _process_validation_request(self, model_id, is_inference):
        """Process a validation request.

        :param model_id: The model id of the model to be validated.
        :type model_id: str
        :param is_inference: True if the validation is an inference request, False if it is a validation request.
        :type is_inference: bool
        :return: The validation metrics, or None if validation failed.
        :rtype: dict
        """
        # Figure out cmd
        if is_inference:
            cmd = 'infer'
        else:
            cmd = 'validate'

        self._send_status(
            f"Processing {cmd} request for model_id {model_id}")
        self.state = ClientState.validating
        try:
            model = self.get_model(str(model_id))
            inpath = self.helper.get_tmp_path()

            with open(inpath, "wb") as fh:
                fh.write(model.getbuffer())

            _, outpath = tempfile.mkstemp()
            self.dispatcher.run_cmd(f"{cmd} {inpath} {outpath}")

            with open(outpath, "r") as fh:
                validation = json.loads(fh.read())

            os.unlink(inpath)
            os.unlink(outpath)

        except Exception as e:
            print("Validation failed with exception {}".format(e), flush=True)
            raise
            self.state = ClientState.idle
            return None

        self.state = ClientState.idle
        return validation

    def process_request(self):
        """Process training and validation tasks. """
        while True:

            if not self._attached:
                return

            try:
                (task_type, request) = self.inbox.get(timeout=1.0)
                if task_type == 'train':

                    tic = time.time()
                    self.state = ClientState.training
                    model_id, meta = self._process_training_request(
                        request.model_id)
                    processing_time = time.time()-tic
                    meta['processing_time'] = processing_time
                    meta['config'] = request.data

                    if model_id is not None:
                        # Send model update to combiner
                        update = fedn.ModelUpdate()
                        update.sender.name = self.name
                        update.sender.role = fedn.WORKER
                        update.receiver.name = request.sender.name
                        update.receiver.role = request.sender.role
                        update.model_id = request.model_id
                        update.model_update_id = str(model_id)
                        update.timestamp = str(datetime.now())
                        update.correlation_id = request.correlation_id
                        update.meta = json.dumps(meta)
                        # TODO: Check responses
                        _ = self.combinerStub.SendModelUpdate(update)
                        self._send_status("Model update completed.", log_level=fedn.Status.AUDIT,
                                          type=fedn.StatusType.MODEL_UPDATE, request=update)

                    else:
                        self._send_status("Client {} failed to complete model update.",
                                          log_level=fedn.Status.WARNING,
                                          request=request)
                    self.state = ClientState.idle
                    self.inbox.task_done()

                elif task_type == 'validate':
                    self.state = ClientState.validating
                    metrics = self._process_validation_request(
                        request.model_id, request.is_inference)

                    if metrics is not None:
                        # Send validation
                        validation = fedn.ModelValidation()
                        validation.sender.name = self.name
                        validation.sender.role = fedn.WORKER
                        validation.receiver.name = request.sender.name
                        validation.receiver.role = request.sender.role
                        validation.model_id = str(request.model_id)
                        validation.data = json.dumps(metrics)
                        self.str = str(datetime.now())
                        validation.timestamp = self.str
                        validation.correlation_id = request.correlation_id
                        _ = self.combinerStub.SendModelValidation(
                            validation)

                        # Set status type
                        if request.is_inference:
                            status_type = fedn.StatusType.INFERENCE
                        else:
                            status_type = fedn.StatusType.MODEL_VALIDATION

                        self._send_status("Model validation completed.", log_level=fedn.Status.AUDIT,
                                          type=status_type, request=validation)
                    else:
                        self._send_status("Client {} failed to complete model validation.".format(self.name),
                                          log_level=fedn.Status.WARNING, request=request)

                    self.state = ClientState.idle
                    self.inbox.task_done()
            except queue.Empty:
                pass

    def _handle_combiner_failure(self):
        """ Register failed combiner connection."""
        self._missed_heartbeat += 1
        if self._missed_heartbeat > self.config['reconnect_after_missed_heartbeat']:
            self._detach()

    def _send_heartbeat(self, update_frequency=2.0):
        """Send a heartbeat to the combiner.

        :param update_frequency: The frequency of the heartbeat in seconds.
        :type update_frequency: float
        :return: None if the client is detached.
        :rtype: None
        """
        while True:
            heartbeat = fedn.Heartbeat(sender=fedn.Client(
                name=self.name, role=fedn.WORKER))
            try:
                self.connectorStub.SendHeartbeat(heartbeat)
                self._missed_heartbeat = 0
            except grpc.RpcError as e:
                status_code = e.code()
                print("CLIENT heartbeat: GRPC ERROR {} retrying..".format(
                    status_code.name), flush=True)
                self._handle_combiner_failure()

            time.sleep(update_frequency)
            if not self._attached:
                return

    def _send_status(self, msg, log_level=fedn.Status.INFO, type=None, request=None):
        """Send status message.

        :param msg: The message to send.
        :type msg: str
        :param log_level: The log level of the message.
        :type log_level: fedn.Status.INFO, fedn.Status.WARNING, fedn.Status.ERROR
        :param type: The type of the message.
        :type type: str
        :param request: The request message.
        :type request: fedn.Request
        """
        status = fedn.Status()
        status.timestamp = str(datetime.now())
        status.sender.name = self.name
        status.sender.role = fedn.WORKER
        status.log_level = log_level
        status.status = str(msg)
        if type is not None:
            status.type = type

        if request is not None:
            status.data = MessageToJson(request)

        self.logs.append(
            "{} {} LOG LEVEL {} MESSAGE {}".format(str(datetime.now()), status.sender.name, status.log_level,
                                                   status.status))
        _ = self.connectorStub.SendStatus(status)

    def run(self):
        """ Run the client. """
        try:
            cnt = 0
            old_state = self.state
            while True:
                time.sleep(1)
                cnt += 1
                if self.state != old_state:
                    print("{}:CLIENT in {} state".format(datetime.now().strftime(
                        '%Y-%m-%d %H:%M:%S'), ClientStateToString(self.state)), flush=True)
                if cnt > 5:
                    print("{}:CLIENT active".format(
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S')), flush=True)
                    cnt = 0
                if not self._attached:
                    print("Detatched from combiner.", flush=True)
                    # TODO: Implement a check/condition to ulitmately close down if too many reattachment attepts have failed. s
                    self._attach()
                    self._subscribe_to_combiner(self.config)
                if self.error_state:
                    return
        except KeyboardInterrupt:
            print("Ok, exiting..")