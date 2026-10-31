"""Retain normal venv bootstrap using the same refreshed installer wheel."""
from pathlib import Path
import re
import shutil
import sys
import sysconfig


wheel = Path(sys.argv[1])
version = wheel.name.removeprefix('pip-').removesuffix('-py3-none-any.whl')
if not re.fullmatch(r'26[.]2[.]2[+]taskopotamus[.]1', version):
    raise ValueError('Unexpected refreshed installer wheel')
module = Path(sysconfig.get_path('stdlib')) / 'ensurepip'
init = module / '__init__.py'
text, count = re.subn(r'(?m)^_PIP_VERSION = "[^"]+"$',
                     '_PIP_VERSION = "' + version + '"', init.read_text())
if count != 1:
    raise ValueError('Upstream ensurepip wheel selection changed')
for previous in (module / '_bundled').glob('pip-*.whl'):
    previous.unlink()
shutil.copyfile(wheel, module / '_bundled' / wheel.name)
init.write_text(text)
