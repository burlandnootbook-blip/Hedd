import requests
import json
import re
import base64
import uuid
import asyncio
import aiohttp
import random
from urllib.parse import urljoin

# ========== CONFIGURATION ==========
PRODUCT_URL = "https://bluewoodskiarea.myshopify.com/products/barcode-zip-hoodie"
BASE_URL = "https://bluewoodskiarea.myshopify.com"
EMAIL = f"test{random.randint(100000, 999999)}@example.com"
FIRST_NAME = "Test"
LAST_NAME = "User"
ADDRESS = "123 Test St"
CITY = "Test City"
STATE = "CA"
ZIP = "90210"
PHONE = "555-123-4567"
# ====================================

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

def get_variant_id():
    resp = session.get(PRODUCT_URL)
    html = resp.text
    m = re.search(r'<select[^>]*name="id"[^>]*>(.*?)</select>', html, re.DOTALL)
    if m:
        options = re.findall(r'<option[^>]*value="([^"]+)"', m.group(1))
        if options:
            return options[0]
    m = re.search(r'"id":(\d+),.*?"available":true', html)
    if m:
        return m.group(1)
    return "42388187578411"

def add_to_cart(variant_id):
    cart_url = urljoin(BASE_URL, "/cart/add.js")
    payload = {"id": int(variant_id), "quantity": 1}
    resp = session.post(cart_url, json=payload, headers={"Content-Type": "application/json"})
    return resp.status_code == 200

def get_checkout_id():
    cart_resp = session.get(urljoin(BASE_URL, "/cart"))
    m = re.search(r'href="([^"]+)"[^>]*>Checkout<', cart_resp.text)
    if m:
        checkout_url = m.group(1)
        m2 = re.search(r'/checkouts/([^/?]+)', checkout_url)
        if m2:
            return m2.group(1)
    return None

def graphql_request(query, variables):
    resp = session.post(urljoin(BASE_URL, "/checkout"), json={"query": query, "variables": variables})
    return resp.json()

def submit_email(checkout_id, email):
    query = """
    mutation checkoutEmailUpdate($checkoutId: ID!, $email: String!) {
        checkoutEmailUpdate(checkoutId: $checkoutId, email: $email) {
            checkout { id }
            userErrors { message }
        }
    }
    """
    variables = {"checkoutId": f"gid://shopify/Checkout/{checkout_id}", "email": email}
    return graphql_request(query, variables)

def submit_shipping(checkout_id, address):
    query = """
    mutation checkoutShippingAddressUpdate($checkoutId: ID!, $shippingAddress: MailingAddressInput!) {
        checkoutShippingAddressUpdate(checkoutId: $checkoutId, shippingAddress: $shippingAddress) {
            checkout { id }
            userErrors { message }
        }
    }
    """
    variables = {
        "checkoutId": f"gid://shopify/Checkout/{checkout_id}",
        "shippingAddress": {
            "firstName": address["first_name"],
            "lastName": address["last_name"],
            "address1": address["address1"],
            "city": address["city"],
            "province": address["province"],
            "zip": address["zip"],
            "phone": address["phone"],
            "country": "US"
        }
    }
    return graphql_request(query, variables)

def get_shipping_rates(checkout_id):
    query = """
    query checkoutShippingRates($checkoutId: ID!) {
        node(id: $checkoutId) {
            ... on Checkout {
                shippingRates {
                    ready
                    shippingRates {
                        handle
                        title
                        priceV2 { amount }
                    }
                }
            }
        }
    }
    """
    variables = {"checkoutId": f"gid://shopify/Checkout/{checkout_id}"}
    data = graphql_request(query, variables)
    rates = data.get("data", {}).get("node", {}).get("shippingRates", {}).get("shippingRates", [])
    return rates[0]["handle"] if rates else None

def select_shipping(checkout_id, rate_handle):
    query = """
    mutation checkoutShippingLineUpdate($checkoutId: ID!, $shippingRateHandle: String!) {
        checkoutShippingLineUpdate(checkoutId: $checkoutId, shippingRateHandle: $shippingRateHandle) {
            checkout { id }
            userErrors { message }
        }
    }
    """
    variables = {"checkoutId": f"gid://shopify/Checkout/{checkout_id}", "shippingRateHandle": rate_handle}
    return graphql_request(query, variables)

def get_braintree_fingerprint():
    resp = session.get(urljoin(BASE_URL, "/checkout"))
    html = resp.text
    m = re.search(r'client_token["\']?\s*:\s*["\']([^"\']+)["\']', html)
    if not m:
        m = re.search(r'var wc_braintree_client_token\s*=\s*\[\s*"([^"]+)"\s*\]', html)
    if m:
        client_token_b64 = m.group(1)
        try:
            client_token_json = base64.b64decode(client_token_b64).decode('utf-8')
            client_data = json.loads(client_token_json)
            return client_data.get('authorizationFingerprint')
        except:
            pass
    return None

async def tokenize_card_braintree(fp, cc, mm, yy, cvv):
    async with aiohttp.ClientSession() as session:
        sid = str(uuid.uuid4())
        query = """
        mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) {
            tokenizeCreditCard(input: $input) { token }
        }
        """
        payload = {
            'clientSdkMetadata': {'source': 'client', 'integration': 'custom', 'sessionId': sid},
            'query': query,
            'variables': {
                'input': {
                    'creditCard': {'number': cc, 'expirationMonth': mm, 'expirationYear': yy, 'cvv': cvv},
                    'options': {'validate': False}
                }
            }
        }
        headers = {
            'Authorization': f'Bearer {fp}',
            'Braintree-Version': '2018-05-10',
            'Content-Type': 'application/json',
        }
        async with session.post('https://payments.braintree-api.com/graphql', headers=headers, json=payload) as resp:
            if resp.status != 200:
                return None
            res = await resp.json()
            return res.get('data', {}).get('tokenizeCreditCard', {}).get('token')

def submit_order(checkout_id, payment_nonce):
    query = """
    mutation checkoutCompleteWithTokenizedPayment($checkoutId: ID!, $paymentNonce: String!) {
        checkoutCompleteWithTokenizedPayment(checkoutId: $checkoutId, paymentNonce: $paymentNonce) {
            checkout { id }
            userErrors { message }
        }
    }
    """
    variables = {"checkoutId": f"gid://shopify/Checkout/{checkout_id}", "paymentNonce": payment_nonce}
    return graphql_request(query, variables)

def main():
    card = input("Enter card (CC|MM|YY|CVV): ").strip()
    parts = card.split('|')
    if len(parts) != 4:
        print("Invalid format")
        return
    cc, mm, yy, cvv = parts
    if len(mm) == 1:
        mm = "0" + mm
    if "20" not in yy:
        yy = f"20{yy}"

    print("[*] Getting variant ID...")
    variant_id = get_variant_id()
    print(f"[+] Variant ID: {variant_id}")

    print("[*] Adding to cart...")
    if not add_to_cart(variant_id):
        print("❌ Failed to add to cart")
        return
    print("[+] Added to cart")

    print("[*] Getting checkout ID...")
    checkout_id = get_checkout_id()
    if not checkout_id:
        print("❌ Failed to get checkout ID")
        return
    print(f"[+] Checkout ID: {checkout_id}")

    print("[*] Submitting email...")
    submit_email(checkout_id, EMAIL)
    print("[+] Email submitted")

    print("[*] Submitting shipping address...")
    address = {
        "first_name": FIRST_NAME,
        "last_name": LAST_NAME,
        "address1": ADDRESS,
        "city": CITY,
        "province": STATE,
        "zip": ZIP,
        "phone": PHONE
    }
    submit_shipping(checkout_id, address)
    print("[+] Shipping address submitted")

    print("[*] Getting shipping rates...")
    rate_handle = get_shipping_rates(checkout_id)
    if rate_handle:
        select_shipping(checkout_id, rate_handle)
        print(f"[+] Shipping method selected: {rate_handle}")
    else:
        print("[!] No shipping method needed")

    print("[*] Getting Braintree fingerprint...")
    fp = get_braintree_fingerprint()
    if not fp:
        print("❌ Failed to get Braintree fingerprint")
        return
    print("[+] Fingerprint obtained")

    print("[*] Tokenizing card...")
    token = asyncio.run(tokenize_card_braintree(fp, cc, mm, yy, cvv))
    if not token:
        print("❌ Card tokenization failed")
        return
    print(f"[+] Card tokenized: {token}")

    print("[*] Submitting order...")
    result = submit_order(checkout_id, token)
    errors = result.get('data', {}).get('checkoutCompleteWithTokenizedPayment', {}).get('userErrors')
    if errors:
        print(f"❌ Order failed: {errors}")
    else:
        print("✅ Order placed successfully!")

if __name__ == "__main__":
    main()