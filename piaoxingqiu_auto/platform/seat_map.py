from __future__ import annotations

import asyncio
from itertools import groupby

from playwright.async_api import Locator

from piaoxingqiu_auto.domain.seating import Candidate, SeatSelection
from piaoxingqiu_auto.platform.checkout_page import PurchasePage


FIND_VENUE_MAP_JS = """() => {
    const container = document.getElementById('vr-container');
    const cached = window.__piaoxingqiuAutoVenueMap;
    if (cached?.venueBoxSelf?.mapbox?.getContainer?.() === container) return cached;
    const queue = [document.getElementById('app')?._vnode];
    const seen = new WeakSet();
    while (queue.length) {
        const value = queue.shift();
        if (!value || (typeof value !== 'object' && typeof value !== 'function') ||
            seen.has(value)) continue;
        seen.add(value);
        try {
            if (typeof value.spliceSelectedZoneIdsByDisableZone === 'function' &&
                typeof value.venueBoxSelf?.loadSeatsInZoneCodes === 'function' &&
                value.venueBoxSelf?.mapbox?.getContainer?.() === container) {
                window.__piaoxingqiuAutoVenueMap = value;
                return value;
            }
        } catch {}
        if (value instanceof Node) continue;
        let keys = [];
        try { keys = Object.keys(value); } catch {}
        for (const key of keys) {
            let child;
            try { child = value[key]; } catch { continue; }
            if (child && (typeof child === 'object' || typeof child === 'function')) {
                queue.push(child);
            }
        }
    }
    return null;
}"""


LOAD_ZONE_JS = (
    "async target => { const findVenueMap = "
    + FIND_VENUE_MAP_JS
    + ";"
    + """
    const deadline = performance.now() + target.timeoutMs;
    const frame = label => new Promise((resolve, reject) => {
        const remaining = deadline - performance.now();
        if (remaining <= 0) return reject(new Error(label));
        const timer = setTimeout(() => reject(new Error(label)), remaining);
        requestAnimationFrame(() => { clearTimeout(timer); resolve(); });
    });
    const bounded = (promise, label) => new Promise((resolve, reject) => {
        const remaining = deadline - performance.now();
        if (remaining <= 0) return reject(new Error(label));
        const timer = setTimeout(() => reject(new Error(label)), remaining);
        Promise.resolve(promise).then(
            value => { clearTimeout(timer); resolve(value); },
            error => { clearTimeout(timer); reject(error); },
        );
    });
    let venueMap;
    do {
        venueMap = findVenueMap();
        if (venueMap?.venueBoxSelf?.mapbox?.isStyleLoaded?.() === true) break;
        await frame('等待票星球场馆对象超时');
    } while (true);

    const {enabledZoneCodes = []} =
        venueMap.spliceSelectedZoneIdsByDisableZone([target.zoneId]);
    if (!enabledZoneCodes.length) throw new Error('目标看台当前不可售');

    const box = venueMap.venueBoxSelf;
    if (typeof venueMap.clickSeatFeature !== 'function' ||
        typeof venueMap.seatInCart !== 'function') {
        throw new Error('票星球官方选座方法不可用');
    }
    const enabled = properties => properties.enable === true ||
        properties.enable === 'true' || properties.enable === 1 ||
        properties.enable === '1';
    for (const code of enabledZoneCodes) box._cachedZoneCodes?.delete(code);
    if (box.seatData?.features) {
        box.seatData.features = box.seatData.features.filter(
            feature => feature.properties?.zoneConcreteId !== target.zoneId
        );
    }
    const globals = document.getElementById('app')?._vnode?.appContext?.config
        ?.globalProperties;
    let features;
    venueMap.isRefreshing = true;
    try {
        await bounded(new Promise((resolve, reject) => {
            Promise.resolve(
                box.loadSeatsInZoneCodes(enabledZoneCodes, resolve)
            ).catch(reject);
        }), '加载目标看台数据超时');
        do {
            const zoneFeatures = (box.seatData?.features || []).filter(
                feature => feature.properties?.zoneConcreteId === target.zoneId
            );
            features = target.seats.map(seat => zoneFeatures.find(feature => {
                const properties = feature.properties || {};
                return enabled(properties) &&
                    properties.seatConcreteId === seat.seatId &&
                    properties.zoneConcreteId === seat.zoneId &&
                    properties.seatPlanId === seat.planId;
            }));
            if (features.every(Boolean)) break;
            await frame('等待目标座位数据超时');
        } while (true);
    } finally {
        venueMap.isRefreshing = false;
        globals?.$loading?.().hide();
    }

    for (const feature of features) {
        if (!venueMap.seatInCart(feature)) {
            await bounded(
                venueMap.clickSeatFeature(feature),
                '票星球官方选座处理超时',
            );
        }
        while (!venueMap.canClickSeat || !venueMap.seatInCart(feature)) {
            await frame('等待座位选中状态超时');
        }
    }
    return features.length;
}"""
)

RESET_SELECTION_JS = (
    "async timeoutMs => { const findVenueMap = "
    + FIND_VENUE_MAP_JS
    + ";"
    + """
    const deadline = performance.now() + timeoutMs;
    const frame = label => new Promise((resolve, reject) => {
        const remaining = deadline - performance.now();
        if (remaining <= 0) return reject(new Error(label));
        const timer = setTimeout(() => reject(new Error(label)), remaining);
        requestAnimationFrame(() => { clearTimeout(timer); resolve(); });
    });
    let venueMap;
    do {
        venueMap = findVenueMap();
        if (venueMap?.venueBoxSelf?.mapbox?.isStyleLoaded?.() === true) break;
        await frame('等待票星球场馆对象超时');
    } while (true);

    venueMap.cancelAllSeat();
    do {
        await frame('等待清空选座状态超时');
        const confirm = [...document.querySelectorAll('button')].find(
            element => element.innerText?.trim() === '确认选座'
        );
        const disabled = confirm && (
            confirm.disabled || confirm.getAttribute('aria-disabled') === 'true' ||
            /disabled|inactive/.test(String(confirm.className).toLowerCase())
        );
        if (!document.querySelector('.seat-item') && disabled) return true;
    } while (true);
}"""
)


async def select_ready_seats(
    site: PurchasePage,
    selection: SeatSelection,
) -> Locator:
    return await _select_seats(site, selection)


async def reselect_seats(
    site: PurchasePage,
    selection: SeatSelection,
) -> Locator:
    await _reset_selection(site)
    return await _select_seats(site, selection)


async def _select_seats(
    site: PurchasePage,
    selection: SeatSelection,
) -> Locator:
    for _, grouped in groupby(
        selection.candidates,
        key=lambda candidate: candidate.seat.zone_id,
    ):
        candidates = tuple(grouped)
        await _load_zone(site, candidates)

    confirm = await site.wait_confirm_seat()
    if confirm is None:
        raise RuntimeError(
            f"点击 {len(selection.candidates)} 个目标座位后等待“确认选座”启用超时"
        )
    return confirm


async def _reset_selection(site: PurchasePage) -> None:
    try:
        await asyncio.wait_for(
            site.page.evaluate(
                RESET_SELECTION_JS,
                max(250, site.config.browser.timeout_ms - 250),
            ),
            timeout=site.config.browser.timeout_ms / 1000,
        )
    except TimeoutError as exc:
        raise RuntimeError("等待清空原选座状态超时") from exc


async def _load_zone(
    site: PurchasePage,
    candidates: tuple[Candidate, ...],
) -> None:
    candidate = candidates[0]
    try:
        count = await asyncio.wait_for(
            site.page.evaluate(
                LOAD_ZONE_JS,
                {
                    "zoneId": candidate.seat.zone_id,
                    "timeoutMs": max(250, site.config.browser.timeout_ms - 250),
                    "seats": [_seat_target(item) for item in candidates],
                },
            ),
            timeout=site.config.browser.timeout_ms / 1000,
        )
    except TimeoutError as exc:
        raise RuntimeError(
            f"等待加载目标看台“{candidate.seat.zone_name}”的座位数据超时"
        ) from exc
    if not count:
        raise RuntimeError(f"目标看台“{candidate.seat.zone_name}”没有返回座位数据")


def _seat_target(candidate: Candidate) -> dict[str, str]:
    return {
        "zoneId": candidate.seat.zone_id,
        "seatId": candidate.seat.seat_id,
        "planId": candidate.plan_id,
    }
