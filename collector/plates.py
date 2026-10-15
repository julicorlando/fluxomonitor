"""Pilot: fixed plate ROI + Tesseract. Run one process per lane."""
import collections
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import time
import threading
import uuid
from pathlib import Path

import cv2
import pytesseract
import requests

PATTERN = re.compile(r'(?:[A-Z]{3}[0-9]{4}|[A-Z]{3}[0-9][A-Z][0-9]{2})')

def read_plate(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # Bound work even when an uncalibrated channel uses the whole 4K frame.
    h,w = gray.shape[:2]
    scale = min(3,1600/w,1000/h)
    gray = cv2.resize(gray, None, fx=scale, fy=scale)
    gray = cv2.copyMakeBorder(gray,15,15,15,15,cv2.BORDER_CONSTANT,value=220)
    variants = [gray, cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]]
    candidates = []
    for image, psm in [(v, mode) for v in variants for mode in (7,11)]:
        try:
            data = pytesseract.image_to_data(image, config=f'--psm {psm} -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', output_type=pytesseract.Output.DICT, timeout=2)
        except RuntimeError as error:
            if 'timeout' not in str(error).lower():
                raise
            logging.warning('OCR excedeu o limite de tempo; calibre a área da placa')
            break
        # OCR can split a plate into adjacent tokens. Join tokens only within a line.
        lines = collections.defaultdict(list)
        for i, text in enumerate(data['text']):
            text = re.sub('[^A-Z0-9]', '', text.upper())
            confidence = float(data['conf'][i])
            if text and confidence>=0:
                key = (data['block_num'][i], data['par_num'][i], data['line_num'][i])
                lines[key].append((text, confidence))
        for words in lines.values():
            text = ''.join(w[0] for w in words)
            if PATTERN.fullmatch(text):
                candidates.append((text, min(w[1] for w in words)/100))
    return max(candidates, key=lambda c:c[1]) if candidates else None

class Consensus:
    def __init__(self, minimum=3, reset_seconds=8):
        self.minimum=minimum; self.reset_seconds=reset_seconds
        self.votes=collections.deque(maxlen=12); self.last_read=0; self.sent=False
    def observe(self, reading, now):
        if self.last_read and now-self.last_read>self.reset_seconds:
            self.votes.clear(); self.sent=False
        if not reading: return None
        self.last_read=now
        if self.sent: return None
        self.votes.append(reading)
        plate, count=collections.Counter(v[0] for v in self.votes).most_common(1)[0]
        if count>=self.minimum and count/len(self.votes)>=0.7:
            self.sent=True
            return plate, sum(v[1] for v in self.votes if v[0]==plate)/count
        return None

class LatestFrame:
    """Continuously drain RTSP while OCR/upload work, retaining only the latest frame."""
    def __init__(self, source):
        self.source=source; self.frame=None; self.at=0; self.sequence=0
        self.lock=threading.Lock()
        threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        retry_delay=2
        while True:
            cap=cv2.VideoCapture(self.source, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,5000,cv2.CAP_PROP_READ_TIMEOUT_MSEC,5000])
            while True:
                ok,frame=cap.read()
                if not ok: break
                retry_delay=2
                with self.lock:
                    self.frame=frame; self.at=time.monotonic(); self.sequence+=1
            cap.release()
            logging.warning('Vídeo indisponível; tentando reconectar')
            time.sleep(retry_delay)
            retry_delay=min(60,retry_delay*2)
    def get(self):
        with self.lock:
            return self.frame,self.at,self.sequence

def video_report(video):
    _,at,_=video.get()
    age=max(0,time.monotonic()-at) if at else None
    return {'frame_age':age if age is not None and age<=86400 else ''}


def report_video(video,url,token):
    while True:
        try:
            data=video_report(video)
            data['channel']=os.environ.get('PLATES_CHANNEL','')
            response=requests.post(url+'/api/v1/plates/heartbeat/',headers={'Authorization':'Bearer '+token},data=data,timeout=(5,5),allow_redirects=False)
            if response.status_code!=200: logging.warning('Status de vídeo não recebido: HTTP %s',response.status_code)
        except requests.RequestException:
            logging.warning('Status de vídeo: painel indisponível')
        time.sleep(10)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    url=os.environ['PLATES_BASE_URL'].rstrip('/')
    if not url.startswith('https://'): raise SystemExit('Use HTTPS em PLATES_BASE_URL')
    source=os.environ['PLATES_RTSP_URL']; token=os.environ['PLATES_TOKEN']
    roi=json.loads(os.environ['PLATES_ROI']) # normalized [x,y,width,height], manually calibrated
    if len(roi)!=4 or any(not isinstance(v,(int,float)) or not 0<=v<=1 for v in roi) or roi[2]<=0 or roi[3]<=0 or roi[0]+roi[2]>1 or roi[1]+roi[3]>1:
        raise SystemExit('PLATES_ROI inválida')
    path=Path(os.environ.get('PLATES_QUEUE','plates-queue.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create private spool before SQLite opens it; snapshots contain vehicle data.
    fd=os.open(path, os.O_CREAT|os.O_RDWR, 0o600); os.close(fd); os.chmod(path,0o600)
    db=sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS queue (event TEXT PRIMARY KEY, plate TEXT, captured TEXT, confidence REAL, image BLOB)')
    consensus=Consensus()
    video=LatestFrame(source); next_send=0; last_sequence=0
    threading.Thread(target=report_video,args=(video,url,token),daemon=True).start()
    while True:
        now=time.monotonic()
        if now>=next_send:
            pending=db.execute('SELECT * FROM queue ORDER BY rowid LIMIT 1').fetchone()
            next_send=now+2
            if pending:
                event,plate,captured,confidence,image=pending
                try:
                    response=requests.post(url+'/api/v1/plates/ingest/', headers={'Authorization':'Bearer '+token}, data={'event_id':event,'plate':plate,'captured_at':captured,'confidence':confidence,'channel':os.environ.get('PLATES_CHANNEL','')}, files={'image':('capture.jpg',image,'image/jpeg')}, timeout=(5,15), allow_redirects=False)
                    if response.status_code in (200,201):
                        db.execute('DELETE FROM queue WHERE event=?',(event,)); db.commit()
                    else:
                        logging.warning('Envio pendente: HTTP %s',response.status_code); next_send=now+30
                except requests.RequestException:
                    logging.warning('Central indisponível; captura preservada na fila'); next_send=now+30
        frame,frame_at,sequence=video.get()
        if frame is None or sequence==last_sequence or time.monotonic()-frame_at>3:
            time.sleep(0.2); continue
        last_sequence=sequence
        h,w=frame.shape[:2]; x,y,rw,rh=roi
        crop=frame[int(y*h):int((y+rh)*h),int(x*w):int((x+rw)*w)]
        if not crop.size: raise SystemExit('ROI vazia')
        reading=read_plate(crop)
        # Ignore very weak readings before voting.
        confirmed=consensus.observe(reading if reading and reading[1]>=0.5 else None,time.monotonic())
        if confirmed:
            plate,confidence=confirmed
            # Save only the configured region; it should include the plate and front of car.
            scale=min(1,1280/crop.shape[1],960/crop.shape[0])
            snapshot=cv2.resize(crop,None,fx=scale,fy=scale)
            ok,encoded=cv2.imencode('.jpg',snapshot,[cv2.IMWRITE_JPEG_QUALITY,80])
            if ok and len(encoded)<=500000:
                if db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]>=10000:
                    raise SystemExit('Fila cheia; restaure conexão com a central antes de reiniciar')
                db.execute('INSERT INTO queue VALUES (?,?,?,?,?)',(str(uuid.uuid4()),plate,dt.datetime.now(dt.timezone.utc).isoformat(),confidence,encoded.tobytes())); db.commit()
                logging.info('Captura registrada na fila')
        time.sleep(0.25)

if __name__=='__main__':
    main()
