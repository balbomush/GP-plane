"""Installer pip destinations stay in their managed venv, despite host config."""
import base64,csv,hashlib,io,os,re,shlex,subprocess,sys,tempfile,unittest,venv,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def fixture_wheel(root):
    name='gp_pip_isolation_fixture';dist=name+'-1.0.dist-info'
    files={name+'.py':b'VALUE = "owned-only"\n',dist+'/METADATA':b'Metadata-Version: 2.1\nName: gp-pip-isolation-fixture\nVersion: 1.0\n',dist+'/WHEEL':b'Wheel-Version: 1.0\nGenerator: GP-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    record=io.StringIO();writer=csv.writer(record,lineterminator='\n')
    for path,data in files.items():writer.writerow([path,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(),len(data)])
    writer.writerow([dist+'/RECORD','','']);files[dist+'/RECORD']=record.getvalue().encode();wheel=root/(name+'-1.0-py3-none-any.whl')
    with zipfile.ZipFile(wheel,'w') as archive:
        for path,data in files.items():archive.writestr(path,data)
    return wheel

class InstallerPipEnvironmentTests(unittest.TestCase):
    def helper(self):
        script=(ROOT/'scripts/install-linux.sh').read_text(encoding='utf-8')
        match=re.search(r'^pip_user\(\) \{\n.*?^\}',script,re.M|re.S);self.assertIsNotNone(match)
        return script,match.group()

    def test_all_pip_calls_use_controlled_environment(self):
        script,helper=self.helper()
        self.assertIn('env -i "${pip_environment[@]}" "$pip_python" -I -m pip "$@"',helper)
        self.assertIn('PIP_CONFIG_FILE=/dev/null',helper)
        self.assertIn('PIP_CACHE_DIR=',helper);self.assertIn('TMPDIR=',helper)
        self.assertNotRegex(script,r'runuser[^\n]+"[^\n]+" -m pip (?:install|wheel|download|check)')
        self.assertEqual(8,len(re.findall(r'^pip_user ',script,re.M)))

    @unittest.skipUnless(sys.platform=='linux','real Linux Bash/pip destination test')
    def test_real_pip_redirect_config_and_environment_cannot_write_outside_venv(self):
        _,helper=self.helper()
        with tempfile.TemporaryDirectory(prefix='gp-pip-env-') as raw:
            root=Path(raw);runtime=root/'venv';venv.EnvBuilder(with_pip=True).create(runtime);python=runtime/'bin/python';wheel=fixture_wheel(root)
            red=root/'red-outside';sentinel=root/'protected-outside';sentinel.mkdir();(sentinel/'canary').write_bytes(b'original')
            site=runtime/'pip.conf';site.write_text('[install]\ntarget = '+str(red)+'\n')
            control={key:value for key,value in os.environ.items() if not key.upper().startswith('PIP_') and key.upper() not in ('PYTHONHOME','PYTHONPATH')}
            # Red install reproduces the inherited redirect while all actual
            # host config stays disabled. Config-only red is parser-qualified.
            control.update(PIP_CONFIG_FILE=os.devnull,PIP_TARGET=str(red),PIP_CACHE_DIR=str(root/'red-pip-cache'),PIP_DISABLE_PIP_VERSION_CHECK='1')
            subprocess.run([str(python),'-I','-m','pip','install','--no-index','--no-deps','--no-cache-dir',str(wheel)],env=control,check=True,capture_output=True,timeout=45)
            self.assertTrue((red/'gp_pip_isolation_fixture.py').is_file(),'red control must reproduce redirect')
            site.write_text('[install]\ntarget = '+str(sentinel)+'\n');user_home=root/'fixture-home';user_config=user_home/'.config/pip/pip.conf';user_config.parent.mkdir(parents=True);user_config.write_text(site.read_text())
            prepare=root/'prepare'
            for path in (prepare/'python/pip-cache',prepare/'python/pip-tmp',prepare/'runtime/pip-cache',prepare/'runtime/pip-tmp'):path.mkdir(parents=True)
            # Only the privilege switch is mocked: the same UID executes the
            # exact product helper, real env(1), real venv Python and real pip.
            shell='''set -Eeuo pipefail
runuser() { [ "$1" = -u ] && [ "$3" = -- ]; shift 3; "$@"; }
'''+''.join(k+'='+shlex.quote(v)+'\n' for k,v in {'INSTALL_USER':'synthetic','target_home':str(user_home),'prepare_dir':str(prepare),'prepare':str(prepare)}.items())+helper+'\npip_user '+shlex.quote(str(python))+' install --no-index --no-deps '+shlex.quote(str(wheel))+'\npip_user '+shlex.quote(str(python))+' check\n'
            poisoned=dict(os.environ,PIP_CONFIG_FILE=str(user_config),PIP_TARGET=str(sentinel),PIP_PREFIX=str(sentinel),PIP_ROOT=str(sentinel),PIP_USER='1',PYTHONHOME=str(sentinel),PYTHONPATH=str(sentinel))
            subprocess.run(['/bin/bash','-s'],input=shell.encode(),env=poisoned,check=True,capture_output=True,timeout=45)
            self.assertEqual(['canary'],sorted(p.name for p in sentinel.iterdir()));self.assertEqual(b'original',(sentinel/'canary').read_bytes())
            observed=subprocess.check_output([str(python),'-I','-c','import gp_pip_isolation_fixture as f,importlib.metadata as m;assert f.VALUE=="owned-only";assert m.version("gp-pip-isolation-fixture")=="1.0";print(f.__file__)'],text=True,timeout=10).strip()
            self.assertTrue(Path(observed).is_relative_to(runtime))

if __name__=='__main__':unittest.main()
