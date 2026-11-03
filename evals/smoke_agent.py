"""Known answers for evaluating the harness plumbing, not a model baseline."""
import json
from pathlib import Path

root = Path.cwd()
if (root / 'prices.py').exists():
    (root / 'prices.py').write_text('def discount_price(price, percent):\n    return price * (1 - percent / 100)\n')
elif (root / 'text_utils.py').exists():
    with (root / 'text_utils.py').open('a') as file:
        file.write("\nimport re\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n")
elif (root / 'checkout.py').exists():
    (root / 'checkout.py').write_text('from catalog import PRICES\n\ndef total(cart):\n    return sum(PRICES[name] * quantity for name, quantity in cart.items())\n')
elif (root / 'list_utils.py').exists():
    (root / 'list_utils.py').write_text('def stable_unique(values):\n    result = []\n    seen = set()\n    for value in values:\n        if value not in seen:\n            seen.add(value)\n            result.append(value)\n    return result\n')
elif (root / 'numbers.py').exists():
    (root / 'numbers.py').write_text('def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError("lower must not exceed upper")\n    return min(max(value, lower), upper)\n')
elif (root / 'duration.py').exists():
    (root / 'duration.py').write_text('def format_duration(seconds):\n    if seconds < 0:\n        raise ValueError("seconds must be non-negative")\n    hours, remainder = divmod(seconds, 3600)\n    minutes, seconds = divmod(remainder, 60)\n    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"\n')
elif (root / 'csv_utils.py').exists():
    (root / 'csv_utils.py').write_text('import csv\nfrom io import StringIO\n\ndef parse_csv_row(text):\n    rows = list(csv.reader(StringIO(text)))\n    if len(rows) != 1:\n        raise ValueError("expected exactly one row")\n    return rows[0]\n')
elif (root / 'stats.py').exists():
    (root / 'stats.py').write_text('def average(values):\n    values = iter(values)\n    try:\n        total = next(values)\n    except StopIteration:\n        return None\n    count = 1\n    for value in values:\n        total += value\n        count += 1\n    return total / count\n')
print(json.dumps({'type': 'run_finished', 'payload': {'usage': {}}}))
