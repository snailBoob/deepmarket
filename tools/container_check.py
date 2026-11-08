"""Verify the built image with Compose on an isolated CI runner (Docker required)."""

import json
import subprocess
import tempfile
import urllib.error
import urllib.request

COMPOSE_FILE = 'compose.yaml'


def compose(*args):
    return subprocess.check_output(
        ['docker', 'compose', '-p', 'deepmarket-check', '-f', COMPOSE_FILE, *args],
        text=True, timeout=100)


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request('http://127.0.0.1:8740' + path, data=data,
                                 headers={'Content-Type': 'application/json'},
                                 method='PUT' if data else 'GET')
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.read()


if __name__ == '__main__':
    # Start with the legacy service name, then recreate with the shipped file.
    # The same project/volume must preserve state across the naming migration.
    legacy = json.loads(compose('config', '--format', 'json'))
    legacy['services']['deep-market'] = legacy['services'].pop('deepmarket')
    legacy_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json')
    json.dump(legacy, legacy_file)
    legacy_file.flush()
    COMPOSE_FILE = legacy_file.name
    compose('up', '-d', '--wait', '--wait-timeout', '60')
    assert b'empty-add' in request('/')
    assert b'<title>deepmarket</title>' in request('/')
    assert b'openDialog' in request('/static/app.js')
    # Local research uploads are not an endpoint in the public distribution.
    for method in ('GET', 'POST'):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    'http://127.0.0.1:8740/upload', method=method), timeout=10) as response:
                raise AssertionError(f'Unexpected public upload endpoint: {response.status}')
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, f'{method} /upload returned {exc.code}'
    request('/api/tabs')
    assert json.loads(request('/api/settings', {'volumeDotSize': 37}))['volumeDotSize'] == 37
    # Exercise a real WebSocket upgrade without opening exchange subscriptions.
    compose('exec', '-T', 'deep-market', 'python', '-c', '''
import asyncio, aiohttp
from deep_market import __version__
async def check():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect('http://127.0.0.1:8740/ws') as ws:
            message = await ws.receive(timeout=5)
            assert message.type == aiohttp.WSMsgType.TEXT, message
            assert message.json()['version'] == __version__
asyncio.run(check())
''')
    # Recreate, rather than restart, to prove the named volume owns the state.
    compose('stop')
    cid = compose('ps', '-aq', 'deep-market').strip()
    exit_code = subprocess.check_output(
        ['docker', 'inspect', '-f', '{{.State.ExitCode}}', cid], text=True).strip()
    assert exit_code == '0', f'Unclean shutdown: {exit_code}'
    # Read this container's lifecycle logs before removal deletes them.
    logs = compose('logs', '--no-color')
    for event in ['starting version=', 'revision=', 'browser_ws_connected', 'browser_ws_disconnected']:
        assert event in logs, event
    compose('rm', '-f')
    COMPOSE_FILE = 'compose.yaml'
    legacy_file.close()
    compose('up', '-d', '--wait', '--wait-timeout', '60')
    assert json.loads(request('/api/settings'))['volumeDotSize'] == 37
    assert compose('ps', '-q', 'deepmarket').strip()
    print('PASS container HTTP/assets, WebSocket, clean shutdown and volume persistence across service rename')
