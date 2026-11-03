import unittest
from agent_core.editing_rules import _unrequested_voice_capabilities
class VoiceRequirementGateTests(unittest.TestCase):
 def test_style_request_does_not_invent_cloning_requirement(self):
  original={'unsupported':['真人录音、指定人物声音克隆']}
  result=_unrequested_voice_capabilities({'instruction':'重新创作自然日语口播，声音不要机械'},original)
  self.assertEqual(result['unsupported'],[])
  self.assertEqual(original['unsupported'],['真人录音、指定人物声音克隆'])
  self.assertEqual(result['ignored_unrequested_capabilities'][0]['reason'],'voice_capability_not_requested_by_user')
 def test_real_voice_cloning_requirement_is_not_removed(self):
  original={'unsupported':['真人录音、指定人物声音克隆']}
  self.assertEqual(_unrequested_voice_capabilities({'instruction':'要求指定人物声音克隆'},original),original)
 def test_unrelated_unsupported_capabilities_are_preserved(self):
  original={'unsupported':['指定人物声音克隆及剪映专属3D模板','竖排字幕']}
  self.assertEqual(_unrequested_voice_capabilities({'instruction':'自然口播'},original)['unsupported'],original['unsupported'])
