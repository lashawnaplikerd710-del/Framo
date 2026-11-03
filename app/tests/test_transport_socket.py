"""Real loopback sockets: streaming, broken body and separate read/total deadlines."""
import contextlib
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_core.provider import ProviderClient, ProviderError

SSE=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('content-length',0)))
        mode=self.server.mode
        try:
            if mode=='headers':time.sleep(.15)
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            if mode=='partial':self.send_header('Content-Length','10000')
            self.end_headers()
            if mode=='heartbeat':
                for _ in range(30):self.wfile.write(b': keepalive\n\n');self.wfile.flush();time.sleep(.02)
            else:
                self.wfile.write(SSE[:20] if mode=='partial' else SSE);self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError,OSError):pass

class SocketTests(unittest.TestCase):
    def client(self,mode,**options):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True;server.mode=mode
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return ProviderClient({'base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'test',
            'api_key_env':'TEST_KEY','stream':True,'connect_timeout_seconds':1,'read_timeout_seconds':1,
            'total_timeout_seconds':2,**options},key_value='private-test-token')

    def test_live_incremental_sse_and_timings(self):
        c=self.client('normal');self.assertEqual(c.complete([{'role':'user','content':'test'}])['message']['content'],'ok')
        self.assertEqual(c.diagnostics[-1]['response_bytes'],len(SSE));self.assertIsNotNone(c.diagnostics[-1]['first_byte_ms'])

    def test_mid_body_disconnect_is_not_success(self):
        c=self.client('partial')
        with self.assertRaises(ProviderError) as caught:c.complete([{'role':'user','content':'test'}])
        self.assertEqual(caught.exception.category,'response_incomplete')
        self.assertNotIn('private-test-token',json.dumps(c.diagnostics))

    def test_response_header_wait_uses_read_limit(self):
        c=self.client('headers',read_timeout_seconds=.04)
        with self.assertRaises(ProviderError) as caught:c.complete([{'role':'user','content':'test'}])
        self.assertEqual(caught.exception.category,'timeout')

    def test_heartbeats_do_not_defeat_total_deadline(self):
        c=self.client('heartbeat',total_timeout_seconds=.09)
        start=time.monotonic()
        with self.assertRaises(ProviderError) as caught:c.complete([{'role':'user','content':'test'}])
        self.assertEqual(caught.exception.category,'timeout');self.assertLess(time.monotonic()-start,.5)

if __name__=='__main__':unittest.main()
