"""Offline checks for installer source integrity and shared dependency constraints."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'deploy/docker/pip'
spec = importlib.util.spec_from_file_location('installer_build', OWNER / 'build.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class RuntimePackagingTests(unittest.TestCase):
    def test_all_application_images_supply_the_chart_init_binary(self):
        template = (ROOT / 'deploy/helm/taskopotamus/templates/deployments.yaml').read_text()
        self.assertIn('command: [/usr/bin/tini, -g, --]', template)
        for component in ('agent', 'mcp', 'google', 'telegram', 'worker'):
            with self.subTest(component=component):
                final = (ROOT / f'deploy/docker/{component}.Dockerfile').read_text().rsplit('FROM ', 1)[1]
                self.assertIn('tini=0.19.0-r3', final)
                self.assertIn('ln -s /sbin/tini /usr/bin/tini', final)
                self.assertIn('test -x /usr/bin/tini', final)

    def test_postgres_source_defaults_match_networked_chart_and_entrypoint(self):
        dockerfile = (ROOT / 'deploy/docker/postgres.Dockerfile').read_text().replace('\\\n', '')
        socket = dockerfile.split('&& awk ', 1)[1].split('&& ./configure', 1)[0]
        network = next(run for run in dockerfile.split('\nRUN ') if run.startswith('sharedir=')).split('\n\nFROM ', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header = root / 'src/include/pg_config_manual.h'
            header.parent.mkdir(parents=True)
            header.write_text('/* preserve unrelated defaults */\n#define DEFAULT_PGSOCKET_DIR  "/tmp"\n')
            sample = root / 'postgresql.conf.sample'
            sample.write_text("#listen_addresses = 'localhost'  # safe caller default\nport = 5432\n")
            subprocess.run(['sh', '-ec', 'awk ' + socket], cwd=root, check=True, capture_output=True, timeout=10)
            pg_config = root / 'pg_config'
            pg_config.write_text('#!/bin/sh\nprintf "%s\\n" "' + str(root) + '"\n')
            pg_config.chmod(0o755)
            subprocess.run(['sh', '-ec', network.replace('/opt/postgresql/bin/pg_config', str(pg_config))],
                           check=True, capture_output=True, timeout=10)
            self.assertIn('#define DEFAULT_PGSOCKET_DIR "/var/run/postgresql"', header.read_text())
            self.assertIn("listen_addresses = '*'", sample.read_text())
            self.assertIn('port = 5432', sample.read_text())
            self.assertEqual((root / 'postgresql.conf.sample.orig').read_text(),
                             "#listen_addresses = 'localhost'  # safe caller default\nport = 5432\n")

    def test_changed_cached_source_is_rejected_before_use(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'source.whl').write_bytes(b'synthetic-original')
            row = {'filename': 'source.whl', 'sha256': hashlib.sha256(b'synthetic-original').hexdigest()}
            self.assertEqual(installer.fetch(row, directory).read_bytes(), b'synthetic-original')
            (directory / 'source.whl').write_bytes(b'synthetic-substitution')
            with self.assertRaisesRegex(ValueError, 'checksum differs'):
                installer.fetch(row, directory)

    def test_installer_lock_preserves_hashes_and_fixed_vendored_release_versions(self):
        lock = json.loads((OWNER / 'sources.lock.json').read_text())
        vendors = {item['name'].lower(): item['version'] for item in lock['vendored']}
        self.assertEqual({name: vendors[name] for name in ('msgpack', 'urllib3', 'setuptools')},
                         {'msgpack': '1.2.1', 'urllib3': '2.8.0', 'setuptools': '80.10.2'})
        for row in [lock['source']] + [artifact for kind in ('tools', 'vendored')
                for item in lock[kind] for artifact in item['artifacts']]:
            self.assertTrue(row['url'].startswith('https://'))
            self.assertRegex(row['sha256'], '^[a-f0-9]{64}$')
            self.assertEqual(Path(row['filename']).name, row['filename'])

    def test_shared_constraints_keep_the_accepted_test_dependency_versions(self):
        constraints = dict(line.split('==') for line in (ROOT / 'requirements-security.txt').read_text().splitlines()
                           if line and not line.startswith('#'))
        self.assertEqual(constraints, {'msgpack': '1.2.1', 'urllib3': '2.8.0', 'setuptools': '84.2.1'})
        requirements = (ROOT / 'requirements-test.txt').read_text().splitlines()
        self.assertIn('-c requirements-security.txt', requirements)
        self.assertTrue(set(constraints) <= set(requirements))
        # All images must ship the same constraints input that their installer consumes.
        for component in ('agent', 'mcp', 'google', 'telegram', 'worker'):
            dockerfile = (ROOT / f'deploy/docker/{component}.Dockerfile').read_text()
            self.assertIn('COPY requirements', dockerfile)
            self.assertIn('requirements-security.txt', dockerfile)
            self.assertIn('-c ', dockerfile)
