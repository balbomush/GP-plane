"""Mandatory artifact integration entrypoint; no optional artifact skips.

Invoke with a Core-only interpreter, an independent Web-only interpreter and
the Web test directory shipped in its source distribution. Product listeners
communicate through HTTP. Parent-side fixtures never become product adapters.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys

MODULES=(
    'tests.integration.auth_http_cases',
    'tests.integration.split_views',
    'tests.integration.core_web_transport',
    'tests.integration.a5_transport',
    'tests.integration.r1_transport',
    'tests.integration.public_http',
)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web-python',type=Path,required=True)
    parser.add_argument('--web-tests',type=Path,required=True)
    parser.add_argument('--core-wheel',type=Path,required=True)
    parser.add_argument('--web-wheel',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--modules',nargs='+',choices=MODULES,default=list(MODULES),
                        help='Targeted repeats only; report records exact subset')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    web_python=args.web_python.resolve(strict=True)
    web_tests=args.web_tests.resolve(strict=True)
    for name in ('metrics_listener.py','segmented_client.py'):
        if not (web_tests/name).is_file():parser.error('missing Web integration controller: '+name)
    env={key:value for key,value in os.environ.items() if key not in {'PYTHONPATH','PYTHONHOME'}}
    env.update(GP_TEST_INSTALLED='1',GP_TEST_WEB_PYTHON=str(web_python),
               GP_TEST_WEB_METRICS=str(web_tests/'metrics_listener.py'),
               GP_TEST_WEB_SEGMENTED=str(web_tests/'segmented_client.py'))
    identities={}
    for name,python,package,forbidden,wheel in (
        ('core',Path(sys.executable),'gp_control_plane','gp_control_plane_web',args.core_wheel.resolve(strict=True)),
        ('web',web_python,'gp_control_plane_web','gp_control_plane',args.web_wheel.resolve(strict=True)),
    ):
        code="""import importlib,importlib.util,json,sys,hashlib,zipfile
from pathlib import Path
package=importlib.import_module(sys.argv[1]);path=Path(package.__file__).resolve()
assert path.is_relative_to(Path(sys.prefix).resolve()),path
assert importlib.util.find_spec(sys.argv[2]) is None,sys.argv[2]
wheel=Path(sys.argv[3]);package_root=path.parent
with zipfile.ZipFile(wheel) as archive:
 expected={name for name in archive.namelist() if name.startswith(sys.argv[1]+'/') and not name.endswith('/')}
 actual={p.relative_to(package_root.parent).as_posix() for p in package_root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
 assert expected==actual,(expected-actual,actual-expected)
 for name in expected:assert (package_root.parent/name).read_bytes()==archive.read(name),name
print(json.dumps({'interpreter':sys.executable,'module':str(path),'wheel':str(wheel),'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'verified_product_files':len(expected)}))
"""
        result=subprocess.run([str(python),'-I','-c',code,package,forbidden,str(wheel)],env=env,capture_output=True,timeout=15)
        if result.returncode:parser.error(name+' environment is not independently installed: '+result.stderr.decode('utf-8','replace'))
        identities[name]=json.loads(result.stdout)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    log=args.output.with_suffix('.log')
    command=[sys.executable,'-W','error','-m','unittest','-v',*args.modules]
    # Parent tests are source controllers, but products are installed artifacts.
    with log.open('wb') as stream:
        run=subprocess.run(command,cwd=root,env=env,stdout=stream,stderr=stream,timeout=900)
    record={'status':'PASS' if run.returncode==0 else 'FAIL','modules':args.modules,
            'complete_matrix':tuple(args.modules)==MODULES,'identities':identities,
            'web_test_controllers':{name:hashlib.sha256((web_tests/name).read_bytes()).hexdigest() for name in ('metrics_listener.py','segmented_client.py')},
            'log':str(log),'log_sha256':hashlib.sha256(log.read_bytes()).hexdigest(),'exit':run.returncode}
    args.output.write_text(json.dumps(record,indent=2),encoding='utf-8')
    return run.returncode


if __name__=='__main__':raise SystemExit(main())
