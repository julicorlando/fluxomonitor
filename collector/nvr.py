"""Synchronize selected NVD 3116 channels from Fluxo; keep NVR credentials local."""
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests


def rtsp_url(recorder, channel, username, password):
    host=recorder['host']
    if ':' in host: host=f'[{host}]'
    return f"rtsp://{quote(username,safe='')}:{quote(password,safe='')}@{host}:{int(recorder['port'])}/cam/realmonitor?channel={int(channel['channel'])}&subtype={int(channel['stream'])}"


def reconcile(children, channels, recorder, base_url, token, username, password, queue_dir):
    wanted={c['channel']:c for c in channels} if recorder['enabled'] else {}
    configs={number:json.dumps([recorder,c],sort_keys=True) for number,c in wanted.items()}
    for number in list(children):
        process,fingerprint=children[number]
        if number not in configs or configs[number]!=fingerprint or process.poll() is not None:
            stop(process); del children[number]
    for number,c in wanted.items():
        if number in children: continue
        env=os.environ.copy()
        env.update(PLATES_RTSP_URL=rtsp_url(recorder,c,username,password),PLATES_TOKEN=token,PLATES_BASE_URL=base_url,PLATES_ROI=json.dumps(c['roi']),PLATES_CHANNEL=str(number),PLATES_QUEUE=str(queue_dir/f"nvr-{recorder['id']}-ch-{number}.sqlite3"),OPENCV_LOG_LEVEL='SILENT')
        process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('plates.py'))],env=env)
        children[number]=(process,configs[number])
        logging.info('Coletor iniciado para canal %s',number)


def stop(process):
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    base_url=os.environ['PLATES_BASE_URL'].rstrip('/')
    parsed=urlsplit(base_url)
    if parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password:
        raise SystemExit('PLATES_BASE_URL deve ser HTTPS sem credenciais')
    token=os.environ['PLATES_NVR_TOKEN']
    username=os.environ['PLATES_NVR_USERNAME']; password=os.environ['PLATES_NVR_PASSWORD']
    if not token or not username or not password: raise SystemExit('Configure token, usuário e senha localmente')
    queue_dir=Path(os.environ.get('PLATES_QUEUE_DIR','plate-queues'))
    queue_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    children={}; last_config=0
    def shutdown(*_): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,shutdown)
    try:
        while True:
            try:
                response=requests.get(base_url+'/api/v1/plates/collector-config/',headers={'Authorization':'Bearer '+token},timeout=(5,15),allow_redirects=False)
                if response.status_code==401:
                    for process,_ in children.values(): stop(process)
                    children.clear()
                    logging.warning('Token do NVR inválido. Canais parados até a configuração ser corrigida.')
                elif response.status_code==200:
                    data=response.json()
                    reconcile(children,data['channels'],data['recorder'],base_url,token,username,password,queue_dir)
                    last_config=time.monotonic()
                else: logging.warning('Configuração indisponível: HTTP %s',response.status_code)
            except (requests.RequestException,ValueError,KeyError,TypeError):
                logging.warning('Falha ao consultar configuração; sem alteração dos canais')
            if last_config and time.monotonic()-last_config>90:
                for process,_ in children.values(): stop(process)
                children.clear(); logging.warning('Configuração sem atualização há 90 segundos; canais pausados')
            time.sleep(15)
    except KeyboardInterrupt:
        pass
    finally:
        for process,_ in children.values(): stop(process)


if __name__=='__main__': main()
