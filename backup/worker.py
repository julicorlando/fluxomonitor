"""Local PostgreSQL backups, restored and compared against an exported snapshot.
No credential or database row is printed. No production restore operation exists.
"""
import hashlib,json,os,re,subprocess,time,uuid,shutil
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
ZONE=ZoneInfo('America/Sao_Paulo')

def connect(db=None):
    import psycopg2
    return psycopg2.connect(host=os.environ.get('PGHOST','db'),port=os.environ.get('PGPORT','5432'),user=os.environ.get('PGUSER','fluxo'),password=os.environ['PGPASSWORD'],dbname=db or os.environ.get('PGDATABASE','fluxo'),connect_timeout=10,options='-c statement_timeout=600000 -c timezone=UTC -c DateStyle=ISO')

def manifest(connection):
    from psycopg2 import sql
    result={}
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        for (table,) in cursor.fetchall():
            # Order independent checksums of every row, including binary images.
            cursor.execute(sql.SQL("SELECT count(*),coalesce(sum((('x'||substr(md5(row_to_json(t)::text),1,16))::bit(64)::bigint)::numeric),0)::text,coalesce(sum((('x'||substr(md5(row_to_json(t)::text),17,16))::bit(64)::bigint)::numeric),0)::text FROM {} t").format(sql.Identifier('public',table)))
            count,a,b=cursor.fetchone();result[table]={'rows':count,'a':a,'b':b}
    return result

def run_tool(args,db=None,snapshot=None):
    env=os.environ.copy()
    if db:env['PGDATABASE']=db
    subprocess.run(args,env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=7200)

def restore_check(filename,expected,admin):
    from psycopg2 import sql
    target='gesti_verify_'+uuid.uuid4().hex
    if not re.fullmatch(r'gesti_verify_[0-9a-f]{32}',target):raise ValueError('Nome temporário inválido')
    created=False
    try:
        with admin.cursor() as cursor:cursor.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(target)))
        created=True
        run_tool(['pg_restore','--exit-on-error','--no-owner','--no-privileges','--dbname',target,str(filename)],db=target)
        restored=connect(target)
        try:
            actual=manifest(restored)
            if actual!=expected:raise ValueError('Conteúdo restaurado divergente')
        finally:restored.close()
        return actual
    finally:
        if created:
            # This identifier was generated above. Never the production database.
            with admin.cursor() as cursor:cursor.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(target)))

def perform(run_id,admin,root):
    path=root/f'gesti-{run_id}-{uuid.uuid4().hex}.dump'
    temporary=path.with_suffix('.partial');phase='exportação'
    try:
        phase='espaço livre'
        with admin.cursor() as cursor:cursor.execute('SELECT pg_database_size(current_database())');database_bytes=cursor.fetchone()[0]
        if shutil.disk_usage(root).free<max(1024**3,database_bytes*3):raise OSError('Espaço insuficiente para cópia e restauração temporária')
        phase='exportação'
        source=connect();source.set_session(isolation_level='REPEATABLE READ',readonly=True)
        try:
            with source.cursor() as cursor:cursor.execute('SELECT pg_export_snapshot()');snapshot=cursor.fetchone()[0]
            expected=manifest(source)
            run_tool(['pg_dump','--format=custom','--no-owner','--no-privileges','--snapshot',snapshot,'--file',str(temporary)])
        finally:source.close()
        os.chmod(temporary,0o600);temporary.replace(path)
        phase='restauração de teste'
        actual=restore_check(path,expected,admin)
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        with admin.cursor() as cursor:
            cursor.execute("UPDATE flow_backuprun SET status='verified',filename=%s,size_bytes=%s,checksum=%s,verified_at=now(),finished_at=now(),manifest=%s,error='' WHERE id=%s",(path.name,path.stat().st_size,digest.hexdigest(),json.dumps(actual),run_id))
        return True
    except Exception as error:
        path.unlink(missing_ok=True)
        # Exception text from clients may contain credentials; expose stage/type only.
        with admin.cursor() as cursor:cursor.execute("UPDATE flow_backuprun SET status='failed',finished_at=now(),error=%s WHERE id=%s",(f'Não concluído na etapa de {phase} ({type(error).__name__}).',run_id))
        return False
    finally:
        temporary.unlink(missing_ok=True)

def retention(admin,root):
    with admin.cursor() as cursor:
        cursor.execute("SELECT id,filename FROM flow_backuprun WHERE status='verified' ORDER BY verified_at DESC OFFSET 7")
        for ident,name in cursor.fetchall():
            # Only worker-generated files inside this private directory are removed.
            if re.fullmatch(r'gesti-\d+-[0-9a-f]{32}\.dump',name):
                (root/name).unlink(missing_ok=True)
                cursor.execute("UPDATE flow_backuprun SET status='retained_log' WHERE id=%s",(ident,))

def work_once():
    admin=connect();admin.autocommit=True
    try:
        with admin.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(77816,2)')
            if not cursor.fetchone()[0]:return
            cursor.execute("SELECT to_regclass('public.flow_backuprun')")
            if not cursor.fetchone()[0]:return
            # A restarted worker cannot confirm a partial previous attempt.
            cursor.execute("UPDATE flow_backuprun SET status='failed',finished_at=now(),error='Execução interrompida; solicite um novo teste.' WHERE status='running'")
            root=Path(os.environ.get('BACKUP_DIR','/backups'));root.mkdir(parents=True,exist_ok=True);os.chmod(root,0o700)
            for partial in root.glob('gesti-*.partial'):
                if re.fullmatch(r'gesti-\d+-[0-9a-f]{32}\.partial',partial.name):partial.unlink(missing_ok=True)
            cursor.execute("SELECT datname FROM pg_database WHERE datname LIKE 'gesti_verify_%' AND pg_get_userbyid(datdba)=current_user")
            for (orphan,) in cursor.fetchall():
                if orphan!=os.environ.get('PGDATABASE','fluxo') and re.fullmatch(r'gesti_verify_[0-9a-f]{32}',orphan):
                    from psycopg2 import sql
                    cursor.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(orphan)))
            now=datetime.now(ZONE)
            today=now.replace(hour=0,minute=0,second=0,microsecond=0)
            cursor.execute("SELECT EXISTS(SELECT 1 FROM flow_backuprun WHERE created_at>=%s AND actor='system')",(today,));made_today=cursor.fetchone()[0]
            cursor.execute('SELECT EXISTS(SELECT 1 FROM flow_backuprun)');has_any=cursor.fetchone()[0]
            if not has_any or (now.hour*60+now.minute>=150 and not made_today):
                cursor.execute("INSERT INTO flow_backuprun(created_at,status,actor,filename,size_bytes,checksum,manifest,error) VALUES(now(),'pending','system','',0,'','{}','')")
            cursor.execute("SELECT id FROM flow_backuprun WHERE status='pending' ORDER BY id LIMIT 1")
            row=cursor.fetchone()
            if not row:return
            run_id=row[0];cursor.execute("UPDATE flow_backuprun SET status='running' WHERE id=%s",(run_id,))
        verified=perform(run_id,admin,root)
        if verified:retention(admin,root)
        print('Backup',run_id,'verificado' if verified else 'não concluído',flush=True)
    finally:admin.close() # also releases advisory lock

if __name__=='__main__':
    os.umask(0o077)
    while True:
        try:work_once()
        except Exception as error:print('Serviço de backup aguardando:',type(error).__name__,flush=True)
        time.sleep(60)
