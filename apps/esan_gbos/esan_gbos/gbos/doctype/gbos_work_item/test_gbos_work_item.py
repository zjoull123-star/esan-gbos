"""Canonical Frappe test-record policy for GBOS Work Item.

The native permission suites build their complete business graph explicitly.
Frappe loads dependency overrides only from this canonical module name, even
when the actual IntegrationTestCase lives in another test module.
"""

IGNORE_TEST_RECORD_DEPENDENCIES = [
    "DocType",
    "GBOS Team",
    "Integration Request",
    "User",
]
