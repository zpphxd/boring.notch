"""Wiring of pluggable providers. Rebuilt whenever settings change in the
dashboard; tests build their own Deps with fakes."""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

from bruno.config import Settings, get_settings
from bruno.delivery import DeliveryProvider, build_delivery
from bruno.llm import LLMClient, build_llm
from bruno.menu import Menu, get_menu
from bruno.messaging import Messenger, build_messenger
from bruno.payments import PaymentProvider, build_payments
from bruno.pos import POSClient, build_pos


@dataclass
class Deps:
    settings: Settings
    menu: Menu
    llm: LLMClient
    messenger: Messenger
    pos: POSClient
    payments: PaymentProvider
    delivery: DeliveryProvider | None = None
    rng: random.Random = field(default_factory=random.Random)
    sleep: Callable[[float], None] = time.sleep


@lru_cache
def get_deps() -> Deps:
    settings = get_settings()
    return Deps(
        settings=settings,
        menu=get_menu(),
        llm=build_llm(settings),
        messenger=build_messenger(settings),
        pos=build_pos(settings),
        payments=build_payments(settings),
        delivery=build_delivery(settings),
    )
