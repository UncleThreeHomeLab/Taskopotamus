"""Refresh pip with its upstream vendoring tool and checksum-locked inputs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import venv


OWNER = Path(__file__).resolve().parent


def fetch(row, directory):
    path = directory / row['filename']
    if not path.exists():
        with urllib.request.urlopen(row['url'], timeout=60) as response:
            path.write_bytes(response.read())
    if hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
        raise ValueError('Installer source checksum differs: ' + row['filename'])
    return path


def build(directory, output):
    lock = json.loads((OWNER / 'sources.lock.json').read_text())
    directory.mkdir(parents=True, exist_ok=True)
    for row in [lock['source']] + [artifact for kind in ('tools', 'vendored')
            for item in lock[kind] for artifact in item['artifacts']]:
        fetch(row, directory)
    source = directory / 'source'
    source.mkdir()
    with tarfile.open(directory / lock['source']['filename']) as bundle:
        bundle.extractall(source, filter='data')
    source, = source.iterdir()
    tools = directory / 'tools'
    venv.EnvBuilder(with_pip=False).create(tools)
    python = str(tools / 'bin/python')
    environment = dict(os.environ, PIP_NO_INDEX='1', PIP_FIND_LINKS=str(directory),
                       PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_CACHE_DIR='1',
                       PATH=str(tools / 'bin') + os.pathsep + os.environ['PATH'])
    requirements = directory / 'tools.txt'
    requirements.write_text('\n'.join(item['name'] + '==' + item['version'] + ' ' +
        ' '.join('--hash=sha256:' + artifact['sha256'] for artifact in item['artifacts'])
        for item in lock['tools']))
    bootstrap = next(item for item in lock['tools'] if item['name'] == 'pip')['artifacts'][0]['filename']
    subprocess.run([python, '-m', 'pip', 'install', '--require-hashes', '-r', str(requirements)],
                   env=dict(environment, PYTHONPATH=str(directory / bootstrap)), check=True)
    environment.pop('PYTHONPATH', None)
    msgpack = next(item for item in lock['vendored'] if item['name'] == 'msgpack')['artifacts'][0]['filename']
    subprocess.run([python, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                    str(directory / msgpack), '-w', str(directory)],
                   env=dict(environment, MSGPACK_PUREPYTHON='1'), check=True)
    vendor = source / 'src/pip/_vendor'
    (vendor / 'vendor.txt').write_text('\n'.join(item['name'] + '==' + item['version']
        for item in lock['vendored']) + '\n')
    # Keep upstream compatibility patches; rebase its pkg_resources patch onto the fixed release.
    shutil.copyfile(OWNER / 'pkg_resources.patch', source / 'tools/vendoring/patches/pkg_resources.patch')
    subprocess.run([str(tools / 'bin/vendoring'), 'sync'], cwd=source, env=environment, check=True)
    init = source / 'src/pip/__init__.py'
    text, count = re.subn(r'(?m)^__version__ = "[^"]+"$',
                         '__version__ = "' + lock['version'] + '"', init.read_text())
    if count != 1:
        raise ValueError('Upstream pip version declaration changed')
    init.write_text(text)
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([python, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                    str(source), '-w', str(output)], env=environment, check=True)


if __name__ == '__main__':
    build(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
