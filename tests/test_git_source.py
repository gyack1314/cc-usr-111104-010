"""核对本地来源对象生成及分支移动。"""
import tempfile,unittest
from pathlib import Path
from source_lab.git_sources import create,git
class GitSourceTests(unittest.TestCase):
    def test_objects(self):
        with tempfile.TemporaryDirectory() as d:
            c=create(d);root=Path(c['root'])
            self.assertEqual(git(root,'rev-parse','main'),c['initial'])
            self.assertIn('160000 commit',git(root,'ls-tree','main'))
            git(root,'update-ref','refs/heads/main',c['moved'])
            self.assertEqual(git(root,'rev-parse','main'),c['moved'])
            self.assertIn('reference',git(root,'ls-tree',c['initial']))
            self.assertIn('sensor',git(root.parent/'middle.git','ls-tree',c['middle']))
