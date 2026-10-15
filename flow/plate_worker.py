"""Integrated singleton supervisor and per-channel OCR processes. No credentials in argv/logs."""
import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone
from .models import PlateRecorder, PlateCamera, PlateCapture
from .plate_credentials import decrypt_password


def channel_plan():
    plan={}
    for camera in PlateCamera.objects.select_related('recorder').filter(enabled=True,recorder__enabled=True,recorder__integrated_enabled=True).exclude(recorder__username='').exclude(recorder__password_encrypted=''):
        nvr=camera.recorder
        # Fingerprint only: do not send credentials to child command arguments or logs.
        fingerprint=hashlib.sha256(json.dumps([nvr.host,nvr.port,nvr.username,nvr.password_encrypted,camera.channel,camera.stream,camera.roi],sort_keys=True).encode()).hexdigest()
        plan[camera.pk]=fingerprint
    return plan


def terminate(process):
    if process.poll() is None:
        process.terminate()
        try:process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill();process.wait()


def sync_children(children,plan):
    for pk in list(children):
        process,fingerprint=children[pk]
        if plan.get(pk)!=fingerprint or process.poll() is not None:
            terminate(process);del children[pk]
    for pk,fingerprint in plan.items():
        if pk not in children:
            process=subprocess.Popen([sys.executable,str(settings.BASE_DIR/'manage.py'),'read_plate_channel',str(pk)])
            children[pk]=(process,fingerprint)
            logging.info('Leitura integrada iniciada no canal cadastrado %s',pk)


def supervise():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    logging.info('Coletor integrado iniciado; aguardando NVRs e canais configurados no painel')
    stopped=threading.Event();children={}
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stopped.set())
    try:
        while not stopped.is_set():
            try:
                close_old_connections()
                plan=channel_plan()
                # Never expose raw exception text: some driver errors may contain configuration.
                sync_children(children,plan)
                PlateRecorder.objects.filter(enabled=True,integrated_enabled=True).exclude(username='').exclude(password_encrypted='').update(last_seen=timezone.now())
            except Exception as error:
                logging.warning('Supervisor de placas aguardando banco/configuração (%s)',type(error).__name__)
                for process,_ in children.values():terminate(process)
                children.clear();connections.close_all()
            stopped.wait(10)
    finally:
        for process,_ in children.values():terminate(process)


def save_pending(db,camera_id):
    pending=db.execute('SELECT event,plate,captured,confidence,image FROM queue ORDER BY rowid LIMIT 1').fetchone()
    if not pending:return
    event,plate,captured,confidence,image=pending
    # Authorization is rechecked on each insert; disabling in the panel stops new writes immediately.
    camera=PlateCamera.objects.select_related('recorder').get(pk=camera_id)
    if not camera.enabled or not camera.recorder.enabled or not camera.recorder.integrated_enabled:return
    PlateCapture.objects.get_or_create(camera_id=camera_id,event_id=event,defaults={'plate':plate,'captured_at':captured,'confidence':confidence,'image':image})
    PlateCamera.objects.filter(pk=camera_id).update(last_seen=timezone.now())
    db.execute('DELETE FROM queue WHERE event=?',(event,));db.commit()


def read_channel(camera_id):
    # Optional heavy dependencies load only in the dedicated plates container.
    from collector.plates import LatestFrame, Consensus, read_plate, video_report
    from collector.nvr import rtsp_url
    import cv2
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    stopped=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stopped.set())
    db=None
    try:
        camera=PlateCamera.objects.select_related('recorder').get(pk=camera_id)
        nvr=camera.recorder
        if not nvr or not camera.enabled or not nvr.enabled or not nvr.integrated_enabled:return
        password=decrypt_password(nvr.password_encrypted)
        if not nvr.username or not password:return
        source=rtsp_url({'host':nvr.host,'port':nvr.port},{'channel':camera.channel,'stream':camera.stream},nvr.username,password)
        roi=camera.roi or [0,0,1,1]
        if len(roi)!=4 or any(not isinstance(v,(float,int)) or not 0<=v<=1 for v in roi) or roi[2]<=0 or roi[3]<=0 or roi[0]+roi[2]>1 or roi[1]+roi[3]>1:raise ValueError('ROI')
        path=Path(os.environ.get('PLATES_QUEUE_DIR','plate-queues'))
        path.mkdir(mode=0o700,parents=True,exist_ok=True)
        filename=path/f'camera-{camera_id}.sqlite3'
        fd=os.open(filename,os.O_CREAT|os.O_RDWR,0o600);os.close(fd);os.chmod(filename,0o600)
        db=sqlite3.connect(filename)
        db.execute('CREATE TABLE IF NOT EXISTS queue (event TEXT PRIMARY KEY, plate TEXT, captured TEXT, confidence REAL, image BLOB)')
        video=LatestFrame(source);consensus=Consensus();last_sequence=0;next_status=0
        while not stopped.is_set():
            now=time.monotonic()
            if now>=next_status:
                age=video_report(video)['frame_age'];checked=timezone.now()
                values={'video_checked_at':checked,'video_status':'streaming' if age!='' and age<=3 else 'unavailable'}
                if age!='':values['last_frame_at']=checked-timedelta(seconds=age)
                try:
                    close_old_connections()
                    active=PlateCamera.objects.filter(pk=camera_id,enabled=True,recorder__enabled=True,recorder__integrated_enabled=True)
                    if not active.update(**values):break
                    next_status=now+10
                except Exception as error:
                    logging.warning('Status do canal %s pendente (%s)',camera_id,type(error).__name__);connections.close_all();next_status=now+5
            try:save_pending(db,camera_id)
            except Exception as error:
                logging.warning('Captura do canal %s preservada na fila (%s)',camera_id,type(error).__name__);connections.close_all()
            frame,frame_at,sequence=video.get()
            if frame is None or sequence==last_sequence or time.monotonic()-frame_at>3:
                stopped.wait(.25);continue
            last_sequence=sequence
            h,w=frame.shape[:2];x,y,rw,rh=roi
            crop=frame[int(y*h):int((y+rh)*h),int(x*w):int((x+rw)*w)]
            if not crop.size:raise ValueError('ROI')
            reading=read_plate(crop)
            confirmed=consensus.observe(reading if reading and reading[1]>=.5 else None,time.monotonic())
            if confirmed:
                plate,confidence=confirmed
                scale=min(1,1280/crop.shape[1],960/crop.shape[0])
                snapshot=cv2.resize(crop,None,fx=scale,fy=scale)
                ok,encoded=cv2.imencode('.jpg',snapshot,[cv2.IMWRITE_JPEG_QUALITY,80])
                if ok and len(encoded)<=500000:
                    if db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]>=10000:raise RuntimeError('Fila cheia')
                    db.execute('INSERT INTO queue VALUES (?,?,?,?,?)',(str(uuid.uuid4()),plate,timezone.now().isoformat(),confidence,encoded.tobytes()));db.commit()
                    logging.info('Captura integrada registrada no canal %s',camera_id)
            stopped.wait(.25)
    except Exception as error:
        logging.error('Canal %s interrompido (%s). Confira configuração e armazenamento.',camera_id,type(error).__name__)
    finally:
        if db:db.close()
        connections.close_all()
