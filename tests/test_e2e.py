"""End-to-end tests: verify exit codes, recover, and resume paths."""

import os
import shutil
import tempfile
import unittest

import tests.fixtures as fx
from hdvrescue import verify, recover, cli, model, TS


def write_tmp(data, suffix=".mpeg"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.write(fd, data)
    os.close(fd)
    return path


class TestVerifyExitCodes(unittest.TestCase):
    def setUp(self):
        self._paths = []

    def tearDown(self):
        for p in self._paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def tmp(self, data):
        p = write_tmp(data)
        self._paths.append(p)
        return p

    def test_exit_0_when_timecode_present(self):
        self.assertEqual(verify.main([self.tmp(fx.recording()), "--quiet"]), 0)

    def test_exit_1_when_absent(self):
        data = fx.recording(with_aux=False)
        self.assertEqual(verify.main([self.tmp(data), "--quiet"]), 1)

    def test_exit_2_on_tiny_file(self):
        self.assertEqual(verify.main([self.tmp(b"\x47\x00\x00\x10"), "--quiet"]), 2)

    def test_exit_2_on_missing_file(self):
        self.assertEqual(verify.main(["/nonexistent/file.m2t", "--quiet"]), 2)


class TestRecover(unittest.TestCase):
    def setUp(self):
        self.outdir = tempfile.mkdtemp()
        self.srcdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.outdir, ignore_errors=True)
        shutil.rmtree(self.srcdir, ignore_errors=True)

    def _src(self, name, data):
        p = os.path.join(self.srcdir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_multi_source_over_record_recover(self):
        # Source A: rec1 (Oct18) + junk + rec2 (Mar02 residue).
        a = (fx.recording(date=(2007, 10, 18), start_time=(9, 14, 3), n_frames=20,
                          pcr_step=90000)
             + fx.junk_bytes(15000)
             + fx.recording(date=(2007, 3, 2), start_time=(14, 2, 13), n_frames=15,
                            pcr_start=7_000_000, pcr_step=90000))
        # Source B: continuation of rec1 (Oct18, ~21s later).
        b = fx.recording(date=(2007, 10, 18), start_time=(9, 14, 23), n_frames=15,
                         pcr_start=3000, pcr_step=90000)
        pa = self._src("CLIP001.mpeg", a)
        pb = self._src("CLIP002.mpeg", b)

        report, plan_obj, results = recover.recover(
            [pa, pb], self.outdir, do_verify=True)

        names = sorted(os.path.basename(r["name"]) for r in results)
        self.assertIn("2007-10-18_09-14-03.m2t", names)   # rec1 across A+B
        self.assertIn("2007-03-02_14-02-13.m2t", names)   # residue split out
        # The Oct18 output spans two sources (a cross-source join).
        oct_out = next(o for o in plan_obj.outputs
                       if o.name == "2007-10-18_09-14-03.m2t")
        self.assertEqual(len(oct_out.members), 2)
        self.assertEqual(oct_out.members[1].join.provenance, "cross-source")
        # Every output verifies.
        for r in results:
            self.assertTrue(r["verified"])
        # report.json and plan.json are written for audit.
        self.assertTrue(os.path.isfile(os.path.join(self.outdir, "report.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.outdir, "plan.json")))

    def test_resume_from_report_and_plan(self):
        p = self._src("X.mpeg", fx.recording())
        recover.recover([p], self.outdir)
        rep_path = os.path.join(self.outdir, "report.json")
        plan_path = os.path.join(self.outdir, "plan.json")

        # from-report: re-plan + build without rescanning.
        out2 = tempfile.mkdtemp()
        try:
            recover.recover([p], out2, from_report=rep_path)
            self.assertTrue(any(n.endswith(".m2t") for n in os.listdir(out2)))
        finally:
            shutil.rmtree(out2, ignore_errors=True)

        # from-plan: build only.
        out3 = tempfile.mkdtemp()
        try:
            # plan references the report beside it; copy both.
            shutil.copy(rep_path, os.path.join(out3, "report.json"))
            recover.recover([p], out3, from_report=os.path.join(out3, "report.json"),
                            from_plan=plan_path)
            self.assertTrue(any(n.endswith(".m2t") for n in os.listdir(out3)))
        finally:
            shutil.rmtree(out3, ignore_errors=True)


class TestCliDispatch(unittest.TestCase):
    def test_full_cli_pipeline(self):
        srcdir = tempfile.mkdtemp()
        outdir = tempfile.mkdtemp()
        try:
            src = os.path.join(srcdir, "a.mpeg")
            with open(src, "wb") as f:
                f.write(fx.recording())
            report_path = os.path.join(outdir, "report.json")
            plan_path = os.path.join(outdir, "plan.json")
            self.assertEqual(cli.main(["scan", src, "-o", report_path]), 0)
            self.assertEqual(cli.main(["plan", report_path, "-o", plan_path]), 0)
            self.assertEqual(cli.main(["build", plan_path, "--report", report_path,
                                       "-o", outdir]), 0)
            built = [n for n in os.listdir(outdir) if n.endswith(".m2t")]
            self.assertEqual(len(built), 1)
            self.assertEqual(cli.main(["verify", os.path.join(outdir, built[0]),
                                       "--quiet"]), 0)
        finally:
            shutil.rmtree(srcdir, ignore_errors=True)
            shutil.rmtree(outdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
