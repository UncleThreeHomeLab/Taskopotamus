"""Exercise the refreshed installer and default venv entirely offline."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


def check(python):
    subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
    code = '''import importlib.metadata,json
from pathlib import Path
import pip
from pip._vendor import msgpack, urllib3, pkg_resources
assert importlib.metadata.version('pip') == '26.2.2+taskopotamus.1'
assert msgpack.__version__ == '1.2.1' and urllib3.__version__ == '2.8.0'
assert msgpack.unpackb(msgpack.packb({'synthetic': True})) == {'synthetic': True}
vendor = Path(pip.__file__).parent / '_vendor'
expected = {'msgpack': '1.2.1', 'urllib3': '2.8.0', 'setuptools': '80.10.2'}
records = {row['name'].lower(): row.get('version') for row in json.loads((vendor / 'bom.cdx.json').read_text())['components']}
assert all(records[name] == version for name, version in expected.items())
assert (vendor / 'pkg_resources/LICENSE').exists() or (vendor / 'pkg_resources/LICENSE.txt').exists()
'''
    subprocess.run([str(python), '-c', code], check=True)


check(sys.executable)
with tempfile.TemporaryDirectory(prefix='installer-proof-') as directory:
    root = Path(directory)
    environment = root / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True)
    python = environment / 'bin/python'
    check(python)
    name = 'synthetic_runtime_probe-0.0.0-py3-none-any.whl'
    wheel = root / name
    metadata = 'synthetic_runtime_probe-0.0.0.dist-info/'
    with zipfile.ZipFile(wheel, 'w') as bundle:
        bundle.writestr('synthetic_runtime_probe.py', 'value = 42\n')
        bundle.writestr(metadata + 'METADATA', 'Metadata-Version: 2.1\nName: synthetic-runtime-probe\nVersion: 0.0.0\n')
        bundle.writestr(metadata + 'WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        bundle.writestr(metadata + 'RECORD', '\n'.join(item + ',,' for item in bundle.namelist()) + '\n' + metadata + 'RECORD,,\n')
    # Exercise a local PEP 517 build as well as installation, without an index or build dependencies.
    project = root / 'project'
    project.mkdir()
    shutil.copyfile(wheel, project / name)
    (project / 'pyproject.toml').write_text('[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n')
    (project / 'backend.py').write_text('import pathlib,shutil\n'
        'def build_wheel(wheel_directory,config_settings=None,metadata_directory=None):\n'
        f' name={name!r}\n shutil.copyfile(pathlib.Path(__file__).parent/name,pathlib.Path(wheel_directory)/name)\n return name\n')
    subprocess.run([str(python), '-m', 'pip', 'install', '--no-index', '--no-deps',
                    '--no-build-isolation', str(project)], check=True)
    subprocess.run([str(python), '-c', 'import synthetic_runtime_probe; assert synthetic_runtime_probe.value == 42'], check=True)
    subprocess.run([str(python), '-m', 'pip', 'uninstall', '--yes', 'synthetic-runtime-probe'], check=True)
    subprocess.run([str(python), '-c', 'import importlib.util; assert importlib.util.find_spec("synthetic_runtime_probe") is None'], check=True)
print('Offline pip, vendored libraries, SBOM, default venv and source installation verified')
