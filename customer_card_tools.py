import time

_STUB = (
    "Al Fardan Exchange does not perform this action on the voice demo line. "
    "Use the knowledge base for general information, or offer branch contact details from RAG."
)


async def verify_customer_by_cnic(cnic: str) -> dict:
    print(f"→ verify_customer_by_cnic (stub): {cnic} @ {time.time()}")
    return {
        "success": False,
        "error": "not_supported",
        "message": _STUB,
    }


async def confirm_physical_custody(cnic: str, has_card: bool) -> dict:
    print(f"→ confirm_physical_custody (stub): {cnic} @ {time.time()}")
    return {
        "success": False,
        "error": "not_supported",
        "message": _STUB,
    }


async def verify_tpin(cnic: str, tpin: str) -> dict:
    print(f"→ verify_tpin (stub): {cnic} @ {time.time()}")
    return {
        "success": False,
        "error": "not_supported",
        "message": _STUB,
    }


async def verify_card_details(cnic: str, last_four_digits: str, expiry_date: str) -> dict:
    print(f"→ verify_card_details (stub): {cnic} @ {time.time()}")
    return {
        "success": False,
        "error": "not_supported",
        "message": _STUB,
    }


async def activate_card(cnic: str) -> dict:
    print(f"→ activate_card (stub): {cnic} @ {time.time()}")
    return {
        "success": False,
        "error": "not_supported",
        "message": _STUB,
    }


async def update_customer_tpin(cnic: str, new_tpin: str) -> dict:
    print(f"→ update_customer_tpin (stub): {cnic} @ {time.time()}")
    return {
        "success": False,
        "error": "not_supported",
        "message": _STUB,
    }


async def transfer_to_ivr_for_pin() -> dict:
    print(f"→ transfer_to_ivr_for_pin (stub) @ {time.time()}")
    return {
        "success": False,
        "error": "not_supported",
        "message": "PIN or card services are not available on this demo. Offer branch or customer portal guidance from the knowledge base.",
    }


async def transfer_to_agent(cnic: str, reason: str) -> dict:
    print(f"→ transfer_to_agent: reason={reason} @ {time.time()}")
    return {
        "success": True,
        "transfer_initiated": True,
        "reason": reason,
        "message": (
            "Transfer to a human representative noted for the contact center. "
            "In production this would connect the caller to an agent queue."
        ),
    }


async def get_customer_status(cnic: str) -> dict:
    print(f"→ get_customer_status (stub): {cnic} @ {time.time()}")
    return {
        "success": False,
        "error": "not_supported",
        "message": _STUB,
    }


async def reset_verification_attempts(cnic: str) -> dict:
    print(f"→ reset_verification_attempts (stub): {cnic} @ {time.time()}")
    return {
        "success": False,
        "error": "not_supported",
        "message": _STUB,
    }
