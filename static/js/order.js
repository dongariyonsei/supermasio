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
        const itemNameEn = item.name_en || '';
        itemElement.innerHTML = `
            <div class="order-summary-main">
                <strong>${item.name_kr}</strong>
                ${itemNameEn ? `<span>${itemNameEn}</span>` : ''}
            </div>
            <div class="order-summary-meta">
                <div class="order-summary-mini-controls">
                    <button type="button" onclick="updateQuantity('${itemId}', -1)" aria-label="${item.name_kr} 줄이기">
                        <i class="bi bi-dash"></i>
                    </button>
                    <span>${quantity}</span>
                    <button type="button" onclick="updateQuantity('${itemId}', 1)" aria-label="${item.name_kr} 늘리기">
                        <i class="bi bi-plus"></i>
                    </button>
                </div>
                <strong>${itemTotal.toLocaleString()}원</strong>
            </div>
        `;
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
    if (sessionExpired) {
        alert('이용 시간이 종료되어 주문할 수 없습니다.');
        return;
    }
    const submitButton = document.getElementById('submit-order-btn');
    const couponInput = document.getElementById('coupon-code');
    const couponCode = couponInput ? couponInput.value.trim() : '';

    clearCouponMessage();
    submitButton.disabled = true;
    submitButton.innerHTML = '<span class="spinner-border spinner-border-sm"></span><span>주문 처리 중...</span>';

    const body = {
        'table_id': tableId,
        'menu': JSON.stringify(orderItems)
    };
    if (couponCode) body['coupon_code'] = couponCode;

    try {
        const response = await fetch('/submit_order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams(body)
        });

        if (response.ok) {
            const result = await response.text();
            document.open();
            document.write(result);
            document.close();
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
        submitButton.innerHTML = '<i class="bi bi-check-circle"></i><span>주문하기</span>';
        updateOrderSummary();
    }
}

// ─── 세션 타이머 ───
function handleSessionExpired() {
    sessionExpired = true;
    const overlay = document.getElementById('session-expired-overlay');
    if (overlay) overlay.style.display = 'block';
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
            warningEl.style.display = (remaining * 1000 <= expiringSoonMs) ? 'block' : 'none';
        }
    }

    tick();
    const intervalId = setInterval(tick, 1000);
}

document.addEventListener('DOMContentLoaded', initSessionTimer);
updateOrderSummary();
