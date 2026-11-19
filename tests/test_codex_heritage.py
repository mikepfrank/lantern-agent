import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lantern.__main__ import arguments, main
from lantern.heritage import HERITAGE_TOOL
from lantern.persona import ThreadPersonas
from lantern.supervision import supervised_tools
from lantern.thread_upgrade import upgrade_saved_thread
from test_codex_smoke import reply


@unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "requires native Codex")
class NativeHeritageUpgrade(unittest.TestCase):
    def test_same_thread_can_read_private_fixture_under_revised_persona(self):
        original = "Old private persona, fixture only."
        revised = "Revised private persona, fixture only."
        excerpt = "The name was chosen by a cardboard owl."
        requests, reviews = [], []
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")

        @contextmanager
        def provider(body, headers):
            request = json.loads(body)
            requests.append(request)
            n = len(requests)
            if n == 2:
                item = {"type": "function_call", "name": "read_heritage", "id": "fc_2",
                        "call_id": "read-2", "status": "completed",
                        "arguments": json.dumps({"rationale": "Read the requested private excerpt."})}
            else:
                item = {"type": "message", "role": "assistant", "id": f"msg_{n}", "status": "completed",
                        "content": [{"type": "output_text", "text": "Original answer." if n == 1 else "Done."}]}
            yield reply(n, item)

        def screen(packet):
            reviews.append(packet)
            return {"escalate": False, "evidence": "", "summary": "A private fixture was requested."}

        with TemporaryDirectory() as tmp:
            state = Path(tmp).resolve()
            archive = state / "private-archive"
            archive.mkdir()
            (archive / "Lantern.md").write_text(revised, encoding="utf-8")
            (archive / "lmstudio-chat.md").write_text(excerpt, encoding="utf-8")
            first_persona = state / "first.md"
            first_persona.write_text(original, encoding="utf-8")

            def say(options, prompt, thread=None):
                argv = ["lantern", "--private-lightsail-cli", "--codex", binary, "--state", str(state),
                        *options, "say", prompt]
                if thread:
                    argv.extend(["--thread", thread])
                with patch.object(sys, "argv", argv), redirect_stdout(StringIO()), redirect_stderr(StringIO()) as log:
                    asyncio.run(main(arguments()))
                return re.search(r"^Thread ([0-9a-f-]+)$", log.getvalue(), re.MULTILINE).group(1)

            with patch.dict(os.environ, OPENROUTER_API_KEY="offline"), \
                 patch("lantern.transport._open_upstream", provider), \
                 patch("lantern.luna.Luna.screen", side_effect=screen), \
                 patch("lantern.review_vendors.request_json", side_effect=AssertionError("Unexpected vendor call")):
                tid = say(["--persona", str(first_persona)], "Quoting our old wording: " + original)
                options = dict(old_persona=original, new_persona=revised,
                               tool=supervised_tools([HERITAGE_TOOL])[0], backup_dir=state / "private-backup")
                report = upgrade_saved_thread(state / "codex", tid, **options)
                self.assertNotEqual(report["previous_sha256"], report["updated_sha256"])
                self.assertEqual(upgrade_saved_thread(state / "codex", tid, **options), report)
                ThreadPersonas(state).replace_pin(tid, hashlib.sha256(original.encode()).hexdigest(), revised)
                self.assertEqual(say(["--heritage-dir", str(archive)], "Read our original naming story.", tid), tid)
                before = next((state / "private-backup").glob("*.jsonl"))
                after = next((state / "codex/sessions").rglob("rollout-*.jsonl"))
                before_lines, after_lines = before.read_bytes().splitlines(), after.read_bytes().splitlines()
                self.assertTrue(all(a == b for i, (a, b) in enumerate(zip(before_lines, after_lines))
                                    if i != 0 and original.encode() not in a))
        self.assertEqual(len(requests), 3)
        self.assertNotIn("read_heritage", {t["name"] for t in requests[0]["tools"]})
        self.assertIn("read_heritage", {t["name"] for t in requests[1]["tools"]})
        history = requests[1]["input"]
        developer = [p["text"] for i in history if i.get("role") == "developer" for p in i["content"]]
        self.assertIn(revised, developer)
        self.assertNotIn(original, developer)
        self.assertIn("Quoting our old wording: " + original, json.dumps(history))
        self.assertIn("Original answer.", json.dumps(history))
        self.assertNotIn(excerpt, json.dumps(requests[:2]))
        outputs = [i for i in requests[2]["input"] if i.get("type") == "function_call_output"]
        self.assertIn(excerpt, json.dumps(outputs))
        self.assertTrue(any(p.get("kind") == "tool" and p.get("tool") == "read_heritage" for p in reviews))
