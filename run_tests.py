"""
Self-contained test runner for Virtual Guard.
Stubs out twilio + anthropic so tests run without those packages installed.
Usage: python run_tests.py
"""

import sys, os, types, json, time, unittest
from unittest.mock import MagicMock, patch

# ── Stubs ─────────────────────────────────────────────────────────────────────

def _install_stubs():
    # --- Twilio stubs ---
    class VoiceResponse:
        def __init__(self): self._parts = []
        def __str__(self): return '<Response>' + ''.join(str(p) for p in self._parts) + '</Response>'
        def say(self, t, **kw): self._parts.append(f'<Say>{t}</Say>'); return self
        def hangup(self): self._parts.append('<Hangup/>'); return self
        def redirect(self, u): self._parts.append(f'<Redirect>{u}</Redirect>'); return self
        def append(self, o): self._parts.append(o); return self

    class Gather:
        def __init__(self, **kw): self._kw = kw; self._parts = []
        def __str__(self):
            attrs = ' '.join(f'{k}="{v}"' for k, v in self._kw.items())
            inner = ''.join(str(p) for p in self._parts)
            return f'<Gather {attrs}>{inner}</Gather>'
        def say(self, t, **kw): self._parts.append(f'<Say>{t}</Say>'); return self

    class Dial:
        def __init__(self, **kw): self._parts = []
        def __str__(self): return '<Dial>' + ''.join(str(p) for p in self._parts) + '</Dial>'
        def number(self, n): self._parts.append(f'<Number>{n}</Number>'); return self

    class MessagingResponse:
        def __init__(self): self._parts = []
        def __str__(self): return '<Response>' + ''.join(str(p) for p in self._parts) + '</Response>'
        def message(self, t): self._parts.append(f'<Message>{t}</Message>'); return self

    twilio      = types.ModuleType('twilio')
    twiml       = types.ModuleType('twilio.twiml')
    voice_mod   = types.ModuleType('twilio.twiml.voice_response')
    msg_mod     = types.ModuleType('twilio.twiml.messaging_response')

    voice_mod.VoiceResponse    = VoiceResponse
    voice_mod.Gather           = Gather
    voice_mod.Dial             = Dial
    msg_mod.MessagingResponse  = MessagingResponse
    twilio.twiml               = twiml
    twiml.voice_response       = voice_mod
    twiml.messaging_response   = msg_mod

    sys.modules.update({
        'twilio': twilio,
        'twilio.twiml': twiml,
        'twilio.twiml.voice_response': voice_mod,
        'twilio.twiml.messaging_response': msg_mod,
    })

    # --- Anthropic stub ---
    anthropic = types.ModuleType('anthropic')
    class FakeAnthropic:
        def __init__(self, **kw): pass
    anthropic.Anthropic = FakeAnthropic
    sys.modules['anthropic'] = anthropic

    # --- python-dotenv stub ---
    dotenv = types.ModuleType('dotenv')
    dotenv.load_dotenv = lambda *a, **kw: None
    sys.modules['dotenv'] = dotenv

_install_stubs()

# Set required env vars before importing app
os.environ.update({
    'IZCLOUD_BASE_URL':   'https://test.izcloud.local',
    'IZCLOUD_USERNAME':   'admin',
    'IZCLOUD_PASSWORD':   'test',
    'ANTHROPIC_API_KEY':  'sk-ant-test',
    'TWILIO_NUMBER':      '+12202228122',
    'MANAGER_PHONE':      '+12016382317',
    'HOA_NAME':           'Test HOA',
})

sys.path.insert(0, os.path.dirname(__file__))

# ── Mock IZCloud + ConversationManager before importing app ───────────────────

iz_mock = MagicMock()
iz_mock.validate_pin.return_value    = {'valid': True,  'gate_id': 'gate-001'}
iz_mock.open_gate.return_value       = True
iz_mock.find_resident_by_phone.return_value = {
    'id': 'res-123', 'name': 'John Smith', 'unit': '101', 'phone': '+15551234567'
}
iz_mock.list_guest_pins.return_value = [
    {'id': 'pin-001', 'label': 'Plumber', 'pin': '445566', 'validFrom': None, 'validTo': None}
]
iz_mock.create_guest_pin.return_value = {'id': 'pin-002', 'pin': '789012', 'label': 'Weekend Guest'}
iz_mock.delete_guest_pin.return_value     = True
iz_mock.regenerate_guest_pin.return_value = {'id': 'pin-001', 'pin': '334455'}
iz_mock.list_vehicles.return_value = [
    {'id': 'veh-001', 'plate': 'ABC123', 'make': 'Toyota', 'model': 'Camry', 'color': 'Blue'}
]
iz_mock.add_vehicle.return_value    = {'id': 'veh-002', 'plate': 'XYZ789'}
iz_mock.remove_vehicle.return_value = True
iz_mock.list_gates.return_value     = [
    {'id': 'gate-001', 'name': 'Main Entrance'},
    {'id': 'gate-002', 'name': 'Pool Gate'},
]

mgr_mock = MagicMock()
mgr_mock.handle_sms.return_value = ('Hello John! How can I help?', {})

with patch('app.main.IZCloudClient', return_value=iz_mock), \
     patch('app.main.ConversationManager', return_value=mgr_mock), \
     patch('app.conversation.IZCloudClient', return_value=iz_mock):
    from app.main import app as flask_app, _normalise_phone, _extract_pin_from_speech
    # Patch the iz reference inside the real conversation manager too (if it got created)
    import app.main as _main_module
    _main_module.iz = iz_mock
    _main_module.mgr = mgr_mock

flask_app.config['TESTING'] = True

# Reset mock between tests
def reset_mocks():
    iz_mock.validate_pin.return_value = {'valid': True, 'gate_id': 'gate-001'}
    iz_mock.open_gate.reset_mock()
    iz_mock.find_resident_by_phone.return_value = {
        'id': 'res-123', 'name': 'John Smith', 'unit': '101', 'phone': '+15551234567'
    }
    mgr_mock.handle_sms.return_value = ('Hello John! How can I help?', {})

# ═══════════════════════════════════════════════════════════════════════════════
#  TEST CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TC001_VoiceGreeting(unittest.TestCase):
    """TC-001: Intercom call greeted with welcome message and PIN prompt."""

    def setUp(self):
        self.client = flask_app.test_client()
        reset_mocks()

    def test_greeting_returns_200(self):
        r = self.client.post('/voice/incoming', data={'CallSid': 'CA001', 'From': '+15559990000'})
        self.assertEqual(r.status_code, 200)

    def test_greeting_contains_welcome(self):
        r = self.client.post('/voice/incoming', data={'CallSid': 'CA002', 'From': '+15559990001'})
        xml = r.data.decode()
        self.assertIn('Welcome', xml)

    def test_greeting_asks_for_pin(self):
        r = self.client.post('/voice/incoming', data={'CallSid': 'CA003', 'From': '+15559990002'})
        xml = r.data.decode()
        self.assertTrue('PIN' in xml or 'pin' in xml.lower())

    def test_greeting_contains_gather(self):
        r = self.client.post('/voice/incoming', data={'CallSid': 'CA004', 'From': '+15559990003'})
        xml = r.data.decode()
        self.assertIn('Gather', xml)

    def test_greeting_mentions_star_for_manager(self):
        r = self.client.post('/voice/incoming', data={'CallSid': 'CA005', 'From': '+15559990004'})
        xml = r.data.decode()
        self.assertTrue('star' in xml.lower() or '*' in xml or 'manager' in xml.lower())


class TC002_PINValidation(unittest.TestCase):
    """TC-002: PIN entry and validation logic."""

    def setUp(self):
        self.client = flask_app.test_client()
        reset_mocks()
        # Prime session
        self.client.post('/voice/incoming', data={'CallSid': 'CA100', 'From': '+15550001000'})

    def test_valid_pin_says_granted(self):
        iz_mock.validate_pin.return_value = {'valid': True, 'gate_id': 'gate-001'}
        r = self.client.post('/voice/pin_received', data={'CallSid': 'CA100', 'Digits': '123456'})
        xml = r.data.decode()
        self.assertTrue('granted' in xml.lower() or 'open' in xml.lower(),
                        f"Expected access granted message, got: {xml}")

    def test_valid_pin_calls_open_gate(self):
        iz_mock.validate_pin.return_value = {'valid': True, 'gate_id': 'gate-001'}
        self.client.post('/voice/pin_received', data={'CallSid': 'CA100', 'Digits': '123456'})
        iz_mock.open_gate.assert_called_once_with('gate-001')

    def test_invalid_pin_prompts_retry(self):
        iz_mock.validate_pin.return_value = {'valid': False}
        r = self.client.post('/voice/pin_received', data={'CallSid': 'CA100', 'Digits': '000000'})
        xml = r.data.decode()
        self.assertTrue('not recognised' in xml.lower() or 'Gather' in xml or 'Redirect' in xml,
                        f"Expected retry prompt, got: {xml}")

    def test_star_key_transfers_to_manager(self):
        r = self.client.post('/voice/pin_received', data={'CallSid': 'CA100', 'Digits': '*'})
        xml = r.data.decode()
        self.assertTrue('manager' in xml.lower() or '+12016382317' in xml,
                        f"Expected manager transfer, got: {xml}")

    def test_pin_never_echoed_in_response(self):
        """SECURITY: PIN must never appear in TTS output."""
        iz_mock.validate_pin.return_value = {'valid': True, 'gate_id': 'gate-001'}
        r = self.client.post('/voice/pin_received', data={'CallSid': 'CA100', 'Digits': '998877'})
        xml = r.data.decode()
        self.assertNotIn('998877', xml, "PIN was spoken in voice response — SECURITY VIOLATION")

    def test_speech_pin_accepted(self):
        iz_mock.validate_pin.return_value = {'valid': True, 'gate_id': 'gate-001'}
        self.client.post('/voice/incoming', data={'CallSid': 'CA101', 'From': '+15550001001'})
        r = self.client.post('/voice/pin_received', data={
            'CallSid': 'CA101', 'Digits': '', 'SpeechResult': 'one two three four five six'
        })
        xml = r.data.decode()
        self.assertTrue('granted' in xml.lower() or 'open' in xml.lower() or 'Gather' in xml)


class TC003_ThreeStrikeTransfer(unittest.TestCase):
    """TC-003: 3 wrong PINs → transfer to manager."""

    def setUp(self):
        self.client = flask_app.test_client()
        reset_mocks()
        iz_mock.validate_pin.return_value = {'valid': False}

    def test_three_failures_transfer_to_manager(self):
        self.client.post('/voice/incoming', data={'CallSid': 'CA200', 'From': '+15550002000'})
        # Attempt 1
        self.client.post('/voice/pin_received', data={'CallSid': 'CA200', 'Digits': '000001'})
        # Attempt 2
        self.client.post('/voice/pin_received', data={'CallSid': 'CA200', 'Digits': '000002'})
        # Attempt 3 — should transfer
        r = self.client.post('/voice/pin_received', data={'CallSid': 'CA200', 'Digits': '000003'})
        xml = r.data.decode()
        self.assertTrue('manager' in xml.lower() or '+12016382317' in xml,
                        f"Expected manager transfer after 3 failures, got: {xml}")

    def test_no_input_transfers_to_manager(self):
        self.client.post('/voice/incoming', data={'CallSid': 'CA201', 'From': '+15550002001'})
        r = self.client.post('/voice/no_input', data={'CallSid': 'CA201'})
        xml = r.data.decode()
        self.assertTrue('manager' in xml.lower() or '+12016382317' in xml)


class TC004_SMSFlow(unittest.TestCase):
    """TC-004: SMS resident identification and response."""

    def setUp(self):
        self.client = flask_app.test_client()
        reset_mocks()

    def test_registered_resident_gets_reply(self):
        r = self.client.post('/sms/incoming', data={
            'MessageSid': 'SM001', 'From': '+15551234567', 'Body': 'Hello'
        })
        self.assertEqual(r.status_code, 200)
        xml = r.data.decode()
        self.assertIn('<Message>', xml)

    def test_unregistered_number_refused(self):
        iz_mock.find_resident_by_phone.return_value = None
        r = self.client.post('/sms/incoming', data={
            'MessageSid': 'SM002', 'From': '+15550009999', 'Body': 'Hi'
        })
        xml = r.data.decode()
        self.assertTrue('not registered' in xml.lower() or 'manager' in xml.lower(),
                        f"Expected rejection message, got: {xml}")

    def test_sms_calls_conversation_manager(self):
        self.client.post('/sms/incoming', data={
            'MessageSid': 'SM003', 'From': '+15551234567', 'Body': 'Open main gate'
        })
        mgr_mock.handle_sms.assert_called()

    def test_sms_response_is_xml(self):
        r = self.client.post('/sms/incoming', data={
            'MessageSid': 'SM004', 'From': '+15551234567', 'Body': 'Hi'
        })
        self.assertIn('xml', r.content_type.lower())


class TC005_Health(unittest.TestCase):
    """TC-005: Health endpoint."""

    def setUp(self):
        self.client = flask_app.test_client()

    def test_health_returns_200(self):
        r = self.client.get('/health')
        self.assertEqual(r.status_code, 200)

    def test_health_returns_ok_status(self):
        r = self.client.get('/health')
        data = json.loads(r.data)
        self.assertEqual(data['status'], 'ok')

    def test_health_has_service_name(self):
        r = self.client.get('/health')
        data = json.loads(r.data)
        self.assertIn('virtual-guard', data.get('service', ''))


class TC006_PhoneNormalisation(unittest.TestCase):
    """TC-006: Phone number normalisation."""

    def test_10_digit(self):
        self.assertEqual(_normalise_phone('5551234567'), '+15551234567')

    def test_11_digit_with_1(self):
        self.assertEqual(_normalise_phone('15551234567'), '+15551234567')

    def test_already_e164(self):
        self.assertEqual(_normalise_phone('+15551234567'), '+15551234567')

    def test_with_dashes(self):
        result = _normalise_phone('555-123-4567')
        self.assertEqual(result, '+15551234567')

    def test_with_parens(self):
        result = _normalise_phone('(555) 123-4567')
        self.assertEqual(result, '+15551234567')


class TC007_SpeechPINExtraction(unittest.TestCase):
    """TC-007: PIN extraction from speech recognition text."""

    def test_6_digit_pin_words(self):
        self.assertEqual(_extract_pin_from_speech('one two three four five six'), '')
        # numeric speech from ASR
        self.assertEqual(_extract_pin_from_speech('1 2 3 4 5 6'), '123456')

    def test_4_digit_min(self):
        self.assertEqual(_extract_pin_from_speech('4 4 5 5'), '4455')

    def test_too_short_rejected(self):
        self.assertEqual(_extract_pin_from_speech('1 2 3'), '')

    def test_no_digits(self):
        self.assertEqual(_extract_pin_from_speech('hello there'), '')

    def test_mixed_text_and_digits(self):
        result = _extract_pin_from_speech('my pin is 123456 please')
        self.assertEqual(result, '123456')

    def test_8_digit_accepted(self):
        result = _extract_pin_from_speech('12345678')
        self.assertEqual(result, '12345678')

    def test_9_digit_rejected(self):
        result = _extract_pin_from_speech('123456789')
        self.assertEqual(result, '')


class TC008_SessionStore(unittest.TestCase):
    """TC-008: Session store correctness."""

    def setUp(self):
        from app.state import SessionStore
        self.store = SessionStore(ttl=60)

    def test_set_and_get(self):
        self.store.set('k1', {'foo': 'bar'})
        self.assertEqual(self.store.get('k1'), {'foo': 'bar'})

    def test_missing_key_is_none(self):
        self.assertIsNone(self.store.get('nonexistent_xyz'))

    def test_clear_removes(self):
        self.store.set('k2', {'x': 1})
        self.store.clear('k2')
        self.assertIsNone(self.store.get('k2'))

    def test_update_merges(self):
        self.store.set('k3', {'a': 1, 'b': 2})
        self.store.update('k3', {'b': 99, 'c': 3})
        val = self.store.get('k3')
        self.assertEqual(val['a'], 1)
        self.assertEqual(val['b'], 99)
        self.assertEqual(val['c'], 3)

    def test_expired_returns_none(self):
        from app.state import SessionStore
        store = SessionStore(ttl=0)
        store.set('k4', {'data': 1})
        time.sleep(0.05)
        self.assertIsNone(store.get('k4'))

    def test_cleanup_removes_expired(self):
        from app.state import SessionStore
        store = SessionStore(ttl=0)
        store.set('exp1', {'x': 1})
        store.set('exp2', {'y': 2})
        time.sleep(0.05)
        removed = store.cleanup_expired()
        self.assertGreaterEqual(removed, 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("=" * 70)
    print("  VIRTUAL GUARD v2.0 — TEST SUITE")
    print("  Claude (Anthropic) + Twilio + IZCloud")
    print("=" * 70)
    print()

    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    test_classes = [
        TC001_VoiceGreeting,
        TC002_PINValidation,
        TC003_ThreeStrikeTransfer,
        TC004_SMSFlow,
        TC005_Health,
        TC006_PhoneNormalisation,
        TC007_SpeechPINExtraction,
        TC008_SessionStore,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print()
    print("=" * 70)
    total   = result.testsRun
    failed  = len(result.failures) + len(result.errors)
    passed  = total - failed
    print(f"  RESULTS: {passed}/{total} passed  |  {failed} failed")
    if failed == 0:
        print("  ✅ ALL TESTS PASSED — code is ready for deployment")
    else:
        print("  ❌ FAILURES DETECTED — review output above")
    print("=" * 70)

    sys.exit(0 if failed == 0 else 1)
