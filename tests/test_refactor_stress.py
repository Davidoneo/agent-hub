"""Contention and rollback checks for the shared backend helpers."""
import concurrent.futures
import io
import sqlite3
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_refactor_contracts import (BASELINE, ROOT, functions, observe, Path, os, re,
    unicodedata, hashlib, shutil, HTTPException)


class RefactorStress(unittest.TestCase):
    def compare(self, scenario):
        actual = observe(ROOT, scenario)
        if BASELINE != ROOT: self.assertEqual(actual, observe(BASELINE, scenario))
        return actual

    def test_shared_whisper_initializes_once_under_contention(self):
        def scenario(root, folder):
            constructions = []
            gate = threading.Barrier(16)
            database = folder / 'test.sqlite3'
            connections = []
            def db():
                conn = sqlite3.connect(database, timeout=10, check_same_thread=False)
                connections.append(conn)
                return conn
            with db() as conn:
                conn.execute('CREATE TABLE meetings (id,transcript_path,updated_at)')
                conn.executemany('INSERT INTO meetings VALUES (?,?,?)', [(str(i),'','') for i in range(16)])
            class Model:
                def __init__(self,*a,**kw):
                    constructions.append(1);time.sleep(0.025)
                def transcribe(self,*a,**kw):
                    return iter([SimpleNamespace(start=0.,end=1.,text=' pronto ')]),None
            ns={'Path':Path,'os':os,'subprocess':SimpleNamespace(run=lambda *a,**kw:None),
                '_WHISPER_MODEL':None,'_WHISPER_MODEL_LOCK':threading.Lock(),
                'MEETING_WHISPER_CACHE':str(folder/'cache'),'MEETING_WHISPER_MODEL':'tiny',
                'db':db,'now':lambda:'fixed','_meeting_error':lambda *a: self.fail(str(a))}
            functions(root,'app/main.py',{'local_transcription_model','_local_transcribe','_transcribe_audio'},ns)
            def invoke(i):
                directory=folder/str(i);directory.mkdir();audio=directory/'audio.wav';audio.write_bytes(b'fake')
                gate.wait(timeout=10)
                return ns['_local_transcribe'](str(audio)) if i%2 else ns['_transcribe_audio']({'id':str(i),'audio_path':str(audio)})
            with patch.dict('sys.modules',{'faster_whisper':SimpleNamespace(WhisperModel=Model)}):
                with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                    results=list(pool.map(invoke,range(16)))
            for conn in connections: conn.close()
            return {'constructors':len(constructions),'results':results,
                    'transcripts':sorted(p.read_text() for p in folder.rglob('transcript.txt'))}
        result=self.compare(scenario)
        self.assertEqual(result['constructors'],1)
        self.assertEqual(result['results'],[True,'pronto']*8)
        self.assertEqual(result['transcripts'],['[0000.0 - 0001.0]  pronto\n']*8)

    def test_failed_catalog_insert_rolls_back_without_changing_cleanup_policy(self):
        def scenario(root,folder):
            conn=sqlite3.connect(':memory:')
            conn.execute('CREATE TABLE documents(id PRIMARY KEY,name,original_name,path,project_slug,scope,size CHECK(size<=2),sha256,content_type,created_at)')
            ns={'Path':Path,'os':os,'re':re,'unicodedata':unicodedata,'hashlib':hashlib,'shutil':shutil,
                'HTTPException':HTTPException,'CONFIG':{'uploads':str(folder),'max_upload':100},
                'ALLOWED_DOC_EXT':{'.txt':'text/plain'},'uuid':SimpleNamespace(uuid4=lambda:'id'),
                'now':lambda:'fixed','db':lambda:conn}
            functions(root,'app/main.py',{'safe_name','doc_ext','insert_document','store_upload'},ns)
            with self.assertRaises(sqlite3.IntegrityError):
                ns['store_upload'](SimpleNamespace(filename='file.txt',file=io.BytesIO(b'123')),'')
            result={'rows':conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0],
                    'files':[(str(p.relative_to(folder)),p.read_text()) for p in folder.rglob('*.txt')]}
            conn.close();return result
        result=self.compare(scenario)
        self.assertEqual(result,{'rows':0,'files':[['id/file.txt','123']]})
