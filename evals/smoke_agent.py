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
print(json.dumps({'type': 'run_finished', 'payload': {'usage': {}}}))
