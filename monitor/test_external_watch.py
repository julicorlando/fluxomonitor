import os,unittest
from unittest.mock import patch
from datetime import datetime,timezone
from monitor import external_watch as watch

class ExternalTests(unittest.TestCase):
    @patch('monitor.external_watch.github')
    @patch.dict(os.environ,{'GITHUB_REPOSITORY':'owner/repo'})
    def test_opens_one_incident_and_only_when_unhealthy(self,github):
        github.return_value=[];watch.report(False,'HTTP 503')
        self.assertEqual(github.call_args_list[1].args[0:2],('POST','/issues'))
        self.assertEqual(github.call_args_list[1].args[2]['assignees'],['owner'])
        github.reset_mock();github.return_value=[{'title':watch.TITLE,'body':watch.MARKER,'number':1}]
        watch.report(False,'HTTP 503');self.assertEqual(github.call_count,1)
    @patch('monitor.external_watch.github')
    def test_normalization_closes_only_marked_incident(self,github):
        github.return_value=[{'title':watch.TITLE,'body':watch.MARKER,'number':2}]
        watch.report(True,'OK')
        self.assertEqual(github.call_args_list[-1].args[0:2],('PATCH','/issues/2'))
    @patch('monitor.external_watch.urlopen')
    def test_probe_rejects_stale_health(self,open_url):
        import json
        open_url.return_value.__enter__.return_value.status=200
        open_url.return_value.__enter__.return_value.read.return_value=json.dumps({'status':'ok','observed_at':'2020-01-01T00:00:00+00:00'}).encode()
        self.assertFalse(watch.probe()[0])
        open_url.return_value.__enter__.return_value.read.return_value=json.dumps({'status':'ok','observed_at':datetime.now(timezone.utc).isoformat()}).encode()
        self.assertTrue(watch.probe()[0])
if __name__=='__main__':unittest.main()
