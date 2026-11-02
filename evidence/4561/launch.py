# ruff: noqa
import os,pathlib,json,subprocess
root=pathlib.Path(__file__).resolve().parent
home=subprocess.check_output(['mktemp','-d',str(root/'home.XXXXXX')],text=True).strip()
(root/'temp-home-clean.txt').write_text(home+'\n')
env={'HOME':home,'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8','UV_CACHE_DIR':home+'/.cache/uv','UV_PYTHON_INSTALL_DIR':home+'/.local/share/uv/python'}
port=json.loads((root/'launch.json').read_text())['port']
os.chdir(home)
f=open(root/'server-final.log','w');os.dup2(f.fileno(),1);os.dup2(f.fileno(),2)
os.execve('/Users/<operator>/.local/bin/uvx',['uvx','--refresh','--from','protolabs-agent','protoagent','serve','--port',str(port)],env)
