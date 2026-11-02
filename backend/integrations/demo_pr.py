"""A demo pull request for the offline path.

This exists so `pr-review simulate-webhook` produces a real review end to end with no
GitHub credentials: the diff below deliberately contains one issue per specialist, so
the fan-out, the dedup, the severity ordering and the approval console all have
something real to display.
"""

from __future__ import annotations

from backend.models.review import ChangedFile

DEMO_REPO = "acme/shop"
DEMO_PR_NUMBER = 42
DEMO_HEAD_SHA = "9f2c1b7e4d5a6c8b0e1f2a3b4c5d6e7f8091a2b3"
DEMO_BASE_SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"

DEMO_DIFF = """diff --git a/billing/stripe.py b/billing/stripe.py
--- a/billing/stripe.py
+++ b/billing/stripe.py
@@ -1,6 +1,34 @@
 import sqlite3
 
-API_BASE = "https://api.stripe.com"
+STRIPE_API_KEY = "sk_live_51H8xQ2eZvKYlo2C9dK3mNpQr"
 
-def charge(customer_id, amount):
-    return payment_gateway.charge(customer_id, amount)
+def charge_customer(customer_id, amount):
+    conn = sqlite3.connect("billing.db")
+    cursor = conn.cursor()
+    cursor.execute(
+        f"SELECT balance FROM accounts WHERE customer_id = '{customer_id}'"
+    )
+    balance = cursor.fetchone()
+    conn.close()
+    if balance == None:
+        return {"ok": False}
+    return payment_gateway.charge(customer_id, amount)
+
+
+def refund_customer(charge_id, amount=None):
+    try:
+        result = payment_gateway.refund(charge_id, amount)
+    except:
+        pass
+    return result
diff --git a/billing/formatting.py b/billing/formatting.py
--- a/billing/formatting.py
+++ b/billing/formatting.py
@@ -1,3 +1,8 @@
 def money(cents):
     return f"${cents / 100:.2f}"
+
+
+class InvoiceFormatter:
+    def render(self, invoice):
+        return money(invoice.total_cents)
"""

DEMO_CHANGED_FILES = [
    ChangedFile(path="billing/stripe.py", status="modified", additions=28, deletions=4),
    ChangedFile(path="billing/formatting.py", status="modified", additions=5, deletions=0),
]

DEMO_TITLE = "Add customer refund path and invoice formatting"
DEMO_AUTHOR = "dev@acme.test"
