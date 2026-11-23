"""The billing guard refuses any model call whose billing inputs don't all name the lab project.

    python3 -m unittest tests.test_billing_guard -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "controller"))
import models as M  # noqa: E402


SA_URL = ("https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
          "lab-agent@example-lab-project.iam.gserviceaccount.com:generateAccessToken")


def adc(project, impersonated=True, url=SA_URL):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    body = {"type": "impersonated_service_account", "service_account_impersonation_url": url} \
        if impersonated else {"type": "authorized_user"}
    if project:
        body["quota_project_id"] = project
    json.dump(body, f)
    f.close()
    return f.name


GOOD = {"LAB_PROJECT": "example-lab-project", "AGY_ADC_AUTH": "true", "GOOGLE_CLOUD_QUOTA_PROJECT": "example-lab-project"}


class BillingGuard(unittest.TestCase):
    def guard(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return M.AntigravityModel.billing_guard()

    def test_lab_settings_pass(self):
        self.assertEqual(self.guard(dict(GOOD, GOOGLE_APPLICATION_CREDENTIALS=adc("example-lab-project"))),
                         "pay-as-you-go")

    def test_workstation_default_adc_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "quota_project_id is 'some-other-project'"):
            self.guard(dict(GOOD, GOOGLE_APPLICATION_CREDENTIALS=adc("some-other-project")))

    def test_env_pointing_elsewhere_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "GOOGLE_CLOUD_QUOTA_PROJECT"):
            self.guard(dict(GOOD, GOOGLE_CLOUD_QUOTA_PROJECT="some-other-project",
                            GOOGLE_APPLICATION_CREDENTIALS=adc("example-lab-project")))

    def test_plain_user_credentials_are_refused_even_for_the_lab(self):
        with self.assertRaisesRegex(RuntimeError, "not an impersonated"):
            self.guard(dict(GOOD, GOOGLE_APPLICATION_CREDENTIALS=adc("example-lab-project", impersonated=False)))

    def test_impersonating_another_account_is_refused(self):
        other = SA_URL.replace("lab-agent@example-lab-project", "someone@other-project")
        with self.assertRaisesRegex(RuntimeError, "not an impersonated"):
            self.guard(dict(GOOD, GOOGLE_APPLICATION_CREDENTIALS=adc("example-lab-project", url=other)))

    def test_absent_quota_project_falls_back_to_the_checked_env(self):
        self.assertEqual(self.guard(dict(GOOD, GOOGLE_APPLICATION_CREDENTIALS=adc(None))), "pay-as-you-go")

    def test_missing_settings_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, "AGY_ADC_AUTH"):
            self.guard({})

    def test_subscription_needs_the_explicit_flag(self):
        self.assertEqual(self.guard({"LAB_ALLOW_SUBSCRIPTION": "1"}), "subscription")


if __name__ == "__main__":
    unittest.main()
