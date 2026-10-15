"""Regression coverage for full-frame OCR stalls and independent health reporting."""
import threading
import time
from unittest import skipUnless
from unittest.mock import Mock,patch
try:
    import numpy as np
    from collector.plates import read_plate
    OCR_AVAILABLE=True
except ImportError:
    OCR_AVAILABLE=False
from django.test import SimpleTestCase
from flow.plate_worker import report_channel_video,terminate

class PlateRuntimeTests(SimpleTestCase):
    @skipUnless(OCR_AVAILABLE,'Requires collector dependencies')
    @patch('collector.plates.pytesseract.image_to_data')
    def test_full_4k_frame_has_bounded_ocr_size_and_deadline(self,ocr):
        ocr.return_value={'text':[]}
        self.assertIsNone(read_plate(np.zeros((2160,3840,3),dtype=np.uint8)))
        self.assertEqual(ocr.call_count,4)
        for call in ocr.call_args_list:
            self.assertLessEqual(call.args[0].shape[1],1630)
            self.assertLessEqual(call.args[0].shape[0],1030)
            self.assertEqual(call.kwargs['timeout'],2)

    @skipUnless(OCR_AVAILABLE,'Requires collector dependencies')
    @patch('collector.plates.pytesseract.image_to_data',side_effect=RuntimeError('Tesseract process timeout'))
    def test_timeout_skips_remaining_variants_without_crashing_channel(self,ocr):
        self.assertIsNone(read_plate(np.zeros((40,160,3),dtype=np.uint8)))
        self.assertEqual(ocr.call_count,1)

    @skipUnless(OCR_AVAILABLE,'Requires collector dependencies')
    @patch('flow.plate_worker.connections')
    @patch('flow.plate_worker.close_old_connections')
    @patch('flow.plate_worker.PlateCamera')
    def test_health_runs_while_ocr_thread_is_blocked(self,cameras,close,connections):
        stopped=threading.Event();reported=threading.Event()
        video=Mock();video.get.return_value=(None,time.monotonic(),1)
        def update(**values):
            self.assertEqual(values['video_status'],'streaming')
            reported.set();stopped.set();return 1
        cameras.objects.filter.return_value.update.side_effect=update
        thread=threading.Thread(target=report_channel_video,args=(video,1,stopped))
        thread.start()
        try:self.assertTrue(reported.wait(2))
        finally:stopped.set();thread.join(2)
        self.assertFalse(thread.is_alive())
        connections.close_all.assert_called_once()

    @patch('flow.plate_worker.os.killpg')
    def test_orphan_group_cleaned_when_parent_already_exited(self,killpg):
        process=Mock(pid=12345);process.poll.return_value=0
        terminate(process)
        self.assertEqual(killpg.call_count,2)
        process.wait.assert_called_once()
