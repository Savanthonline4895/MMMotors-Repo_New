import os
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from database import calc_gst_line, calc_bill_totals


def test_cgst_plus_sgst_equals_gst_total_for_every_price():
    # ₹1 @ 18% used to print CGST 0.07 + SGST 0.07 against GST 0.15
    for paise in range(100, 200000, 100):
        for rate in (5, 12, 18, 28):
            line = calc_gst_line(paise / 100, 1, rate)
            assert round(line["cgst"] + line["sgst"], 2) == line["gst_total"], (paise, rate, line)
            assert abs(line["cgst"] - line["sgst"]) <= 0.011


def test_one_rupee_at_18_percent():
    line = calc_gst_line(1, 1, 18)
    assert (line["taxable"], line["gst_total"], line["cgst"], line["sgst"]) == (0.85, 0.15, 0.07, 0.08)


def test_bill_totals_breakdown_adds_up():
    t = calc_bill_totals([{"unit_price": 1, "qty": 1, "gst_rate": 18},
                          {"unit_price": 999, "qty": 3, "gst_rate": 28, "discount": 10}])
    halves = sum(s["cgst"] + s["sgst"] for s in t["gst_breakup"].values())
    assert round(halves, 2) == t["gst_total"]
