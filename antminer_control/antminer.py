import json
import os
import re
import socket
import time
from collections import OrderedDict
from pathlib import Path, PosixPath

from paramiko import SSHClient, AutoAddPolicy
from scp import SCPClient


def ssh_client(fn):
    def fn_wrap(self, *args, **kwargs):
        with SSHClient() as client:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(AutoAddPolicy)
            client.connect(self._host, self._ssh_port, self._username, self._password)
            return fn(self, client, *args, **kwargs)

    return fn_wrap


class Antminer:
    CONFIG_FILE_DIR = '/config'
    CONFIG_FILE_NAME = 'cgminer.conf'
    RESTART_COMMAND = '/etc/init.d/cgminer.sh restart'
    TIMEOUT = 5

    def __init__(self, host, ssh_port=22, api_port=4028, username='root', password='admin'):
        self._host = host
        self._ssh_port = ssh_port
        self._api_port = api_port
        self._username = username
        self._password = password

        self._local_config_path = Path(host, self.CONFIG_FILE_NAME)
        self._remote_config_path = PosixPath(self.CONFIG_FILE_DIR, self.CONFIG_FILE_NAME)
        self._make_dir()

        self._config = None  # self.read_config()

    @property
    def config(self):
        if not self._config:
            self._config = self.read_config()
        return self._config

    @property
    def frequency(self):
        return int(self.config['bitmain-freq'])

    @frequency.setter
    def frequency(self, value):
        if self._is_valid_frequency(int(value)):
            self.config['bitmain-freq'] = str(value)
        else:
            raise ValueError('Frequency is not valid: {}'.format(value))

    def _is_valid_frequency(self, value):
        return ((100 <= value <= 400 and value % 25 == 0) or
                (400 <= value <= 700 and value % 6.25 == 0.0))

    @property
    def fan_speed(self):
        return self.config['bitmain-fan-pwm']

    @fan_speed.setter
    def fan_speed(self, value):
        if self._is_valid_fan_speed(int(value)):
            self.config['bitmain-fan-pwm'] = str(value)
        else:
            raise ValueError('Fan speed is not valid:'.format(value))

    def _is_valid_fan_speed(self, value):
        return 0 <= value <= 100

    @property
    def fan_control(self):
        return self.config['bitmain-fan-ctrl']

    @fan_control.setter
    def fan_control(self, value):
        if isinstance(value, bool):
            self.config['bitmain-fan-ctrl'] = value

    @property
    def stats(self):
        return self.send_api_command({'command': 'stats'})['STATS'][1]

    @property
    def temperature(self):
        """Find and return the highest hashing board temperature from api 'stats' call.

        :return: highest temperature of hashing boards
        """
        return max([v for k, v in self.stats.items() if re.fullmatch('temp\d+', k)])

    @property
    def hash_rate_avg(self):
        return self.stats['GHS av']

    @property
    def hash_rate_5s(self):
        return self.stats['GHS 5s']

    @property
    def hardware_error_rate(self):
        return self.stats['Device Hardware%']

    @property
    def api_frequency(self):
        return int(self.stats['frequency'])

    @property
    def summary(self):
        return self.send_api_command({'command': 'summary'})['SUMMARY'][0]

    @property
    def elapsed(self):
        return int(self.summary['Elapsed'])

    def _make_dir(self):
        os.makedirs(self._host, exist_ok=True)

    @ssh_client
    def pull_config(self, client):
        scp = SCPClient(client.get_transport())
        scp.get(str(self._remote_config_path), str(self._local_config_path))
        os.chmod(str(self._local_config_path), 0o777)

    @ssh_client
    def push_config(self, client, restart=False):
        self.write_config()

        scp = SCPClient(client.get_transport())
        scp.put(str(self._local_config_path), str(self._remote_config_path))
        if restart:
            client.exec_command(self.RESTART_COMMAND)
            time.sleep(10)

    def read_config(self, from_local=False):
        if not from_local:
            self.pull_config()
        with open(str(self._local_config_path)) as f:
            conf = json.loads(f.read(), object_pairs_hook=OrderedDict)

        return conf

    def write_config(self):
        if not self._config:
            raise RuntimeError('Config has not been read from device.')
        with open(str(self._local_config_path), 'w') as f:
            f.write(json.dumps(self._config, indent=0))

    def send_api_command(self, cmd, expect_response=True):
        resp = None

        with socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM) as _socket:
            _socket.settimeout(self.TIMEOUT)
            _socket.connect((self._host, self._api_port))
            _socket.send(json.dumps(cmd).encode())

            if expect_response:
                resp = b''
                while True:
                    buf = _socket.recv(4096)
                    if buf:
                        resp += buf
                    else:
                        break

                resp = json.loads(self.fix_json_format(resp.decode()))

        return resp

    def fix_json_format(self, bad_json: str):
        return bad_json.replace('}{', '},{').strip(' \0')