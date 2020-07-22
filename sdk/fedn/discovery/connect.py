import enum

import requests as r


class State(enum.Enum):
    Disconnected = 0
    Connected = 1
    Error = 2


class DiscoveryClientConnect:

    def __init__(self, host, port, token):
        self.host = host
        self.port = port
        self.token = token
        self.state = State.Disconnected

    def state(self):
        return self.state


class DiscoveryCombinerConnect(DiscoveryClientConnect):

    def __init__(self, host, port, token, myhost, myport, myname):
        super().__init__(host, port, token)
        self.connect_string = "http://{}:{}".format(self.host, self.port)
        self.myhost = myhost
        self.myport = myport
        self.myname = myname
        print("\n\nsetting the connection string to {}\n\n".format(self.connect_string),flush=True)

    def connect(self):

        retval = r.get("{}{}/".format(self.connect_string + '/combiner/', self.myname),
                      headers={'Authorization': 'Token {}'.format(self.token)})

        if retval.status_code != 200:

            #print("Got payload {}".format(ret), flush=True)
            payload = {'name': self.myname, 'port': self.myport, 'host': self.myhost, 'status': "S", 'user': 1}
            retval = r.post(self.connect_string + '/combiner/', data=payload, headers={'Authorization': 'Token {}'.format(self.token)})
            print("status is {} and payload {}".format(retval.status_code, retval.text), flush=True)
            if retval.status_code >= 200 or retval.status_code < 204:
                self.state = State.Connected
            else:
                self.state = State.Disconnected
        else:
            self.state = State.Connected

        return self.state

    def update_status(self, status):
        print("\n\nUpdate status", flush=True)
        payload = {'status': status}
        retval = r.patch("{}{}/".format(self.connect_string  + '/combiner/', self.myname), data=payload,
                        headers={'Authorization': 'Token {}'.format(self.token)})

        print("SETTING UPDATE< WHAT HAPPENS {} {}".format(retval.status_code,retval.text),flush=True)
        if retval.status_code >= 200 or retval.status_code < 204:
            self.state = State.Connected
        else:
            self.state = State.Disconnected

        newstatus = None

        retval = r.get("{}{}/".format(self.connect_string + '/combiner/', self.myname),
                       headers={'Authorization': 'Token {}'.format(self.token)})

        payload = retval.json()
        try:
            newstatus = payload['status']
        except Exception as e:
            print("Error getting payload {}".format(e))
            self.state = State.Error

        return newstatus

    def check_status(self):
        print("\n\nCheck status", flush=True)
        status = None

        retval = r.get("{}{}/".format(self.connect_string + '/combiner/', self.myname),
                       headers={'Authorization': 'Token {}'.format(self.token)})

        payload = retval.json()
        print("Got payload {}".format(payload),flush=True)
        try:
            status = payload['status']
        except Exception as e:
            print("Error getting payload {}".format(e))
            self.state = State.Error

        return status, self.state

    def get_config(self):
        retval = r.get("{}{}/".format(self.connect_string + '/configuration/', self.myname),
                       headers={'Authorization': 'Token {}'.format(self.token)})

        payload = retval.json()
        print("GOT CONFIG: {}".format(payload))

        return payload, self.state

