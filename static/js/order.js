// 메뉴 데이터 초기화
const menuDataElement = document.getElementById('menu-data');
const menuItems = JSON.parse(menuDataElement.dataset.menu);
const tableId = document.getElementById('table-data').dataset.tableId;

let orderItems = {};
let sessionExpired = false;

function updateQuantity(itemId, change) {
    const input = document.getElementById(`quantity-${itemId}`);
    if (!input) return;

    const currentValue = parseInt(input.value) || 0;
    const newValue = Math.max(0, currentValue + change);
    input.value = newValue;

    if (newValue > 0) {
        orderItems[itemId] = newValue;
    } else {
        delete orderItems[itemId];
    }

    updateMenuCardState(itemId, newValue);
    updateOrderSummary();
}

function updateMenuCardState(itemId, quantity) {
    const card = document.querySelector(`.order-menu-card[data-item-id="${itemId}"]`);
    if (!card) return;
    card.classList.toggle('is-selected', quantity > 0);
}

function findMenuItem(itemId) {
    return menuItems[itemId] || null;
}

function updateOrderSummary() {
    const summary = document.getElementById('order-summary');
    const subtotalAmount = document.getElementById('subtotal-amount');
    const totalAmount = document.getElementById('total-amount');
    const submitButton = document.getElementById('submit-order-btn');
    const summaryCount = document.getElementById('summary-count');

    // This script can remain loaded briefly while document.write() swaps in the
    // order-success page. Do not assume order-page nodes still exist.
    if (!summary || !totalAmount || !submitButton) return;
    let total = 0;
    let totalCount = 0;

    summary.innerHTML = '';

    for (const [itemId, quantity] of Object.entries(orderItems)) {
        const item = findMenuItem(itemId);
        if (!item) continue;

        const itemTotal = item.price * quantity;
        total += itemTotal;
        totalCount += quantity;

        const itemElement = document.createElement('div');
        itemElement.className = 'order-summary-item';

        const main = document.createElement('div');
        main.className = 'order-summary-main';
        const nameKr = document.createElement('strong');
        nameKr.textContent = item.name_kr || '';
        main.appendChild(nameKr);
        if (item.name_en) {
            const nameEn = document.createElement('span');
            nameEn.textContent = item.name_en;
            main.appendChild(nameEn);
        }

        const meta = document.createElement('div');
        meta.className = 'order-summary-meta';
        const controls = document.createElement('div');
        controls.className = 'order-summary-mini-controls';

        const plus = document.createElement('button');
        plus.type = 'button';
        plus.setAttribute('aria-label', `${item.name_kr || '메뉴'} 늘리기`);
        plus.addEventListener('click', () => updateQuantity(itemId, 1));
        const plusIcon = document.createElement('i');
        plusIcon.className = 'bi bi-plus';
        plus.appendChild(plusIcon);

        const count = document.createElement('span');
        count.textContent = String(quantity);

        const minus = document.createElement('button');
        minus.type = 'button';
        minus.setAttribute('aria-label', `${item.name_kr || '메뉴'} 줄이기`);
        minus.addEventListener('click', () => updateQuantity(itemId, -1));
        const minusIcon = document.createElement('i');
        minusIcon.className = 'bi bi-dash';
        minus.appendChild(minusIcon);

        controls.appendChild(minus);
        controls.appendChild(count);
        controls.appendChild(plus);

        const price = document.createElement('strong');
        price.textContent = `${itemTotal.toLocaleString()}원`;
        meta.appendChild(controls);
        meta.appendChild(price);

        itemElement.appendChild(main);
        itemElement.appendChild(meta);
        summary.appendChild(itemElement);
    }

    if (totalCount === 0) {
        summary.innerHTML = `
            <div class="order-summary-empty">
                <i class="bi bi-bag"></i>
                <span>아직 담은 메뉴가 없습니다.</span>
            </div>
        `;
    }

    if (subtotalAmount) subtotalAmount.textContent = `${total.toLocaleString()}원`;
    totalAmount.textContent = `${total.toLocaleString()}원`;
    if (summaryCount) summaryCount.textContent = totalCount;
    submitButton.disabled = (total === 0) || sessionExpired;

    updateFloatingCart(total, totalCount);
}

function showCouponMessage(text, isError) {
    const el = document.getElementById('coupon-message');
    if (!el) return;
    el.textContent = text;
    el.style.display = 'block';
    el.className = `small mt-1 ${isError ? 'text-danger' : 'text-success'}`;
}

function clearCouponMessage() {
    const el = document.getElementById('coupon-message');
    if (el) el.style.display = 'none';
}

async function submitOrder() {
    let replacedDocument = false;
    if (sessionExpired) {
        alert('이용 시간이 종료되어 주문할 수 없습니다.');
        return;
    }
    const submitButton = document.getElementById('submit-order-btn');
    const couponInput = document.getElementById('coupon-code');
    const couponCode = couponInput ? couponInput.value.trim() : '';

    clearCouponMessage();
    submitButton.disabled = true;
    submitButton.classList.add('is-loading');
    submitButton.textContent = '주문 처리 중...';

    const body = {
        'table_id': tableId,
        'menu': JSON.stringify(orderItems)
    };
    if (couponCode) body['coupon_code'] = couponCode;
    const takeoutBonusInput = document.querySelector('input[name="takeout_bonus"]:checked');
    if (takeoutBonusInput && takeoutBonusInput.value) body['takeout_bonus'] = takeoutBonusInput.value;

    try {
        const response = await fetch('/submit_order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams(body)
        });

        if (response.ok) {
            replacedDocument = true;
            window.location.assign(response.url || `/order?table=${tableId}`);
            return;
        }

        // 오류 응답 처리 (주문 미생성). 메뉴 선택 상태는 유지됨.
        let detail = null;
        try {
            const data = await response.json();
            detail = data.detail;
        } catch (e) { /* non-JSON */ }

        const errorObj = (detail && typeof detail === 'object') ? detail : null;
        const errorCode = errorObj ? errorObj.error : null;
        const message = errorObj ? errorObj.message : (typeof detail === 'string' ? detail : '주문 처리 중 오류가 발생했습니다.');

        if (errorCode === 'session_expired') {
            handleSessionExpired();
            alert(message);
        } else if (errorCode && errorCode.startsWith('coupon')) {
            showCouponMessage(message, true);
        } else {
            alert(message);
        }
    } catch (error) {
        alert('주문 처리 중 오류가 발생했습니다.');
    } finally {
        if (replacedDocument) return;
        if (submitButton) {
            submitButton.classList.remove('is-loading');
            submitButton.textContent = '주문하기';
        }
        updateOrderSummary();
    }
}

// ─── 세션 타이머 ───
function handleSessionExpired() {
    sessionExpired = true;
    const overlay = document.getElementById('session-expired-overlay');
    if (overlay) overlay.classList.add('is-visible');
    const submitButton = document.getElementById('submit-order-btn');
    if (submitButton) submitButton.disabled = true;
    const timer = document.getElementById('session-timer');
    if (timer) timer.textContent = '00:00';
}

function initSessionTimer() {
    const bar = document.getElementById('session-bar');
    if (!bar) return;

    const expiresAt = new Date(bar.dataset.expiresAt).getTime();
    const serverNow = new Date(bar.dataset.serverNow).getTime();
    const expiringSoonMs = (parseInt(bar.dataset.expiringSoonMinutes) || 10) * 60 * 1000;
    // 클라이언트 로컬 시계와 서버 시계의 오프셋 보정 (로컬 시간을 신뢰하지 않음)
    const offset = Date.now() - serverNow;

    const timerEl = document.getElementById('session-timer');
    const warningEl = document.getElementById('session-warning');

    function tick() {
        const now = Date.now() - offset;
        let remaining = Math.floor((expiresAt - now) / 1000);
        if (remaining <= 0) {
            handleSessionExpired();
            clearInterval(intervalId);
            return;
        }
        const min = Math.floor(remaining / 60);
        const sec = remaining % 60;
        if (timerEl) timerEl.textContent = `${String(min).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
        if (warningEl) {
            warningEl.classList.toggle('is-visible', remaining * 1000 <= expiringSoonMs);
        }
    }

    tick();
    const intervalId = setInterval(tick, 1000);
}

// ─── 플로팅 카트 뱃지 ───
let _cartInView = false;

function updateFloatingCart(total, totalCount) {
    const fc = document.getElementById('floating-cart');
    if (!fc) return;
    const countEl = document.getElementById('fc-count');
    const totalEl = document.getElementById('fc-total');
    if (countEl) countEl.textContent = totalCount;
    if (totalEl) totalEl.textContent = `${total.toLocaleString()}원`;

    const shouldShow = totalCount > 0 && !_cartInView && window.scrollY > 420;
    const wasVisible = fc.classList.contains('is-visible');
    if (shouldShow && !wasVisible) {
        fc.classList.add('is-visible');
    } else if (!shouldShow) {
        fc.classList.remove('is-visible');
    }
}

function scrollToCart() {
    const cart = document.getElementById('order-cart');
    if (cart) cart.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function initFloatingCart() {
    const cart = document.getElementById('order-cart');
    if (!cart || !('IntersectionObserver' in window)) return;
    new IntersectionObserver((entries) => {
        _cartInView = entries[0].isIntersecting;
        const fc = document.getElementById('floating-cart');
        if (!fc) return;
        const hasItems = Object.keys(orderItems).length > 0;
        if (hasItems && !_cartInView && window.scrollY > 420) {
            fc.classList.add('is-visible');
        } else {
            fc.classList.remove('is-visible');
        }
    }, { threshold: 0.15 }).observe(cart);

    window.addEventListener('scroll', () => {
        const totalCount = Object.keys(orderItems).reduce((sum, itemId) => sum + (parseInt(orderItems[itemId]) || 0), 0);
        const total = Object.entries(orderItems).reduce((sum, [itemId, quantity]) => {
            const item = findMenuItem(itemId);
            return sum + (item ? item.price * quantity : 0);
        }, 0);
        updateFloatingCart(total, totalCount);
    }, { passive: true });
}

document.addEventListener('DOMContentLoaded', () => {
    initSessionTimer();
    initFloatingCart();
});
updateOrderSummary();
