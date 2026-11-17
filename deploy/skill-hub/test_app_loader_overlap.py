"""Direct regression tests for deploy/skill-hub/app/skill_index.py."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import skill_index as si


class DeployAppSkillIndexOverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hub = Path(self.temp.name)
        self.skill_dir = self.hub / 'skills' / 'example'
        self.skill_dir.mkdir(parents=True)
        self.doc = self.skill_dir / 'SKILL.md'
        self.doc.write_text(
            '---\nname: example\ndescription: Deploy loader overlap fixture.\n---\n# Procedure\n',
            encoding='utf-8',
        )
        si.build(self.hub)

    def test_get_rejects_overlapping_materialize_paths_on_deploy_app_copy(self):
        expected_mod = (Path(__file__).resolve().parent / 'app' / 'skill_index.py').resolve()
        self.assertEqual(Path(si.__file__).resolve(), expected_mod)
        original = self.doc.read_text(encoding='utf-8')

        # Same path
        with self.assertRaises(ValueError):
            si.get('example', hub=self.hub, materialize_root=self.hub / 'skills')
        self.assertTrue(self.doc.is_file())
        self.assertEqual(self.doc.read_text(encoding='utf-8'), original)

        # Descendant path
        with self.assertRaises(ValueError):
            si.get('example', hub=self.hub, materialize_root=self.skill_dir)
        with self.assertRaises(ValueError):
            si.get('example', hub=self.hub, materialize_root=self.skill_dir / 'sub')
        self.assertFalse((self.skill_dir / 'example').exists())
        self.assertFalse((self.skill_dir / 'sub').exists())

        # Ancestor path
        nested = self.hub / 'skills' / 'skills'
        nested.mkdir(parents=True)
        nested_doc = nested / 'SKILL.md'
        nested_doc.write_text('---\nname: skills\ndescription: Ancestor test\n---\nBody.\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            si.get('skills', hub=self.hub, materialize_root=self.hub)
        self.assertTrue(self.doc.is_file())
        self.assertTrue(nested_doc.is_file())

        # Link-resolved overlap and link target
        link_root = self.hub / 'linked-skills'
        unrelated = self.hub / 'unrelated'
        unrelated.mkdir()
        (unrelated / 'keep.txt').write_text('keep', encoding='utf-8')
        target_link_parent = self.hub / 'mat-with-link'
        target_link_parent.mkdir()
        target_link = target_link_parent / 'example'

        def _make_dir_link(link_path, dest_path):
            if os.name == 'nt':
                res = subprocess.run(
                    ['cmd', '/c', 'mklink', '/J', str(link_path), str(dest_path)],
                    capture_output=True, text=True, check=False,
                )
                return res.returncode == 0
            try:
                os.symlink(dest_path, link_path, target_is_directory=True)
                return True
            except OSError:
                return False

        if _make_dir_link(link_root, self.hub / 'skills'):
            with self.assertRaises(ValueError):
                si.get('example', hub=self.hub, materialize_root=link_root)
            self.assertEqual(self.doc.read_text(encoding='utf-8'), original)

        if _make_dir_link(target_link, unrelated):
            with self.assertRaises(ValueError):
                si.get('example', hub=self.hub, materialize_root=target_link_parent)
            self.assertTrue((unrelated / 'keep.txt').is_file())

        # Normal distinct target and repeated loading
        mat_root = self.hub / 'materialized'
        first = si.get('example', hub=self.hub, materialize_root=mat_root)
        second = si.get('example', hub=self.hub, materialize_root=mat_root)
        self.assertEqual(Path(first['path']).resolve(), (mat_root / 'example').resolve())
        self.assertEqual(Path(second['path']).resolve(), (mat_root / 'example').resolve())
        self.assertTrue(self.doc.is_file())

    def test_get_protects_hub_state_prompts_and_ancestors(self):
        state_skill = self.hub / 'skills' / 'state'
        state_skill.mkdir(parents=True)
        (state_skill / 'SKILL.md').write_text(
            '---\nname: state\ndescription: State collision\n---\nBody.\n', encoding='utf-8'
        )
        prompts_dir = self.hub / 'prompts'
        prompts_dir.mkdir(exist_ok=True)
        (prompts_dir / 'keep.md').write_text('keep', encoding='utf-8')
        db_bytes = si.db_path(self.hub).read_bytes()

        with self.assertRaises(ValueError):
            si.get('state', hub=self.hub, materialize_root=self.hub)
        with self.assertRaises(ValueError):
            si.get('example', hub=self.hub, materialize_root=prompts_dir)
        with self.assertRaises(ValueError):
            si.get('example', hub=self.hub, materialize_root=self.hub)

        self.assertEqual(si.db_path(self.hub).read_bytes(), db_bytes)
        self.assertTrue((prompts_dir / 'keep.md').is_file())
        ok = si.get('example', hub=self.hub, materialize_root=self.hub / 'materialized')
        self.assertTrue((Path(ok['path']) / 'SKILL.md').is_file())

    def test_get_reloads_readonly_and_preserves_prior_on_locked_target(self):
        import stat
        mat_root = self.hub / 'materialized'
        ro_src = self.skill_dir / 'extra.txt'
        ro_src.write_text('v1\n', encoding='utf-8')
        os.chmod(ro_src, stat.S_IREAD)
        self.addCleanup(lambda: os.chmod(ro_src, stat.S_IWRITE | stat.S_IREAD) if ro_src.exists() else None)

        first = si.get('example', hub=self.hub, materialize_root=mat_root)
        target = Path(first['path'])
        os.chmod(ro_src, stat.S_IWRITE | stat.S_IREAD)
        ro_src.write_text('v2\n', encoding='utf-8')
        os.chmod(ro_src, stat.S_IREAD)

        second = si.get('example', hub=self.hub, materialize_root=mat_root)
        self.assertEqual((Path(second['path']) / 'extra.txt').read_text(encoding='utf-8'), 'v2\n')
        self.assertEqual([p.name for p in mat_root.glob('.materializing-*')], [])

        with open(target / 'SKILL.md', 'rb'):
            if os.name == 'nt':
                with self.assertRaises(OSError):
                    si.get('example', hub=self.hub, materialize_root=mat_root)
        self.assertTrue((target / 'SKILL.md').is_file())
        self.assertEqual((target / 'extra.txt').read_text(encoding='utf-8'), 'v2\n')
        self.assertEqual([p.name for p in mat_root.glob('.materializing-*')], [])
        si._make_writable(mat_root)

    def test_get_prompt_fast_path_skips_iterdir_and_preserves_fallbacks(self):
        from unittest.mock import patch

        prompts_dir = self.hub / 'prompts'
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / 'fast-md.md').write_text(
            '---\nname: fast-md\ndescription: Direct md prompt\nprompt_type: task_template\n---\nDirect body.\n',
            encoding='utf-8',
        )
        (prompts_dir / 'fallback-txt.txt').write_text(
            '---\nname: fallback-txt\ndescription: Non-md prompt\nprompt_type: task_template\n---\nFallback body.\n',
            encoding='utf-8',
        )

        with patch.object(Path, 'iterdir', side_effect=AssertionError('iterdir must not run for .md')):
            prompt = si.get_prompt('fast-md', hub=self.hub)
        self.assertIn('Direct body.', prompt['body'])

        fallback = si.get_prompt('fallback-txt', hub=self.hub)
        self.assertIn('Fallback body.', fallback['body'])

        with self.assertRaises(KeyError):
            si.get_prompt('missing-prompt', hub=self.hub)

    def test_get_preserves_sole_backup_when_publish_and_rollback_fail(self):
        import os
        import shutil
        import stat
        from unittest.mock import patch

        skill_dir = self.hub / 'skills' / 'rollback-guard-demo'
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / 'SKILL.md').write_text(
            '---\nname: rollback-guard-demo\ndescription: Backup preservation fixture\n---\n# Good v1\n',
            encoding='utf-8',
        )
        (skill_dir / 'payload.txt').write_text('last-good-v1\n', encoding='utf-8')
        ro_src = skill_dir / 'readonly.txt'
        ro_src.write_text('ro-v1\n', encoding='utf-8')
        os.chmod(ro_src, stat.S_IREAD)
        self.addCleanup(lambda: os.chmod(ro_src, stat.S_IWRITE | stat.S_IREAD) if ro_src.exists() else None)

        mat_root = self.hub / 'materialized'
        first = si.get('rollback-guard-demo', hub=self.hub, materialize_root=mat_root)
        target = Path(first['path'])
        self.assertEqual((target / 'payload.txt').read_text(encoding='utf-8'), 'last-good-v1\n')

        (skill_dir / 'payload.txt').write_text('candidate-v2\n', encoding='utf-8')

        orig_rename = Path.rename

        def _fail_publish_rollback_and_rescue(self_path, dest_path):
            if self_path.parent.name.startswith('.materializing-'):
                raise OSError('injected swap/rollback/rescue failure')
            return orig_rename(self_path, dest_path)

        with patch.object(Path, 'rename', _fail_publish_rollback_and_rescue):
            with self.assertRaises(OSError) as ctx:
                si.get('rollback-guard-demo', hub=self.hub, materialize_root=mat_root)

        staging_dirs = list(mat_root.glob('.materializing-*'))
        self.assertEqual(len(staging_dirs), 1)
        backup_dir = staging_dirs[0] / 'backup'
        self.assertIn(str(backup_dir), str(ctx.exception))
        self.assertTrue((backup_dir / 'SKILL.md').is_file())
        self.assertEqual((backup_dir / 'payload.txt').read_text(encoding='utf-8'), 'last-good-v1\n')
        self.assertEqual((backup_dir / 'readonly.txt').read_text(encoding='utf-8'), 'ro-v1\n')
        self.assertEqual((skill_dir / 'payload.txt').read_text(encoding='utf-8'), 'candidate-v2\n')
        self.assertFalse(target.exists())

        backup_dir.rename(target)
        for residual in list(mat_root.glob('.materializing-*')):
            si._make_writable(residual)
            shutil.rmtree(residual)

        def _competing_publisher(self_path, dest_path):
            if self_path.name == 'staged':
                dest_path.mkdir(parents=True, exist_ok=True)
                (dest_path / 'competing.txt').write_text('competing-winner\n', encoding='utf-8')
                raise FileExistsError('competing target already created')
            if self_path.name == 'backup':
                raise FileExistsError('cannot overwrite competing target')
            return orig_rename(self_path, dest_path)

        with patch.object(Path, 'rename', _competing_publisher):
            with self.assertRaises(OSError) as ctx2:
                si.get('rollback-guard-demo', hub=self.hub, materialize_root=mat_root)

        staging_dirs2 = list(mat_root.glob('.materializing-*'))
        self.assertEqual(len(staging_dirs2), 1)
        backup_dir2 = staging_dirs2[0] / 'backup'
        self.assertIn(str(backup_dir2), str(ctx2.exception))
        self.assertEqual((backup_dir2 / 'payload.txt').read_text(encoding='utf-8'), 'last-good-v1\n')
        self.assertEqual((target / 'competing.txt').read_text(encoding='utf-8'), 'competing-winner\n')

        si._make_writable(mat_root)
        shutil.rmtree(target)
        for residual in list(mat_root.glob('.materializing-*')):
            shutil.rmtree(residual)
        (skill_dir / 'payload.txt').write_text('last-good-v1\n', encoding='utf-8')
        si.get('rollback-guard-demo', hub=self.hub, materialize_root=mat_root)
        (skill_dir / 'payload.txt').write_text('candidate-v2\n', encoding='utf-8')

        def _single_failure_rollback_ok(self_path, dest_path):
            if self_path.name == 'staged':
                raise OSError('injected single publish failure')
            return orig_rename(self_path, dest_path)

        with patch.object(Path, 'rename', _single_failure_rollback_ok):
            with self.assertRaises(OSError):
                si.get('rollback-guard-demo', hub=self.hub, materialize_root=mat_root)
        self.assertEqual((target / 'payload.txt').read_text(encoding='utf-8'), 'last-good-v1\n')
        self.assertEqual(list(mat_root.glob('.materializing-*')), [])
        si._make_writable(mat_root)


if __name__ == '__main__':
    unittest.main()
