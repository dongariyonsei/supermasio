/**
 * order.js — Super Masio Menu/Order Page
 * Handles quantity updates, cart bar, and order submission.
 * Matches Figma frame 4065-1148.
 *
 * Korean & English only — no Chinese.
 */

// ============================================================
// State
// ============================================================
const menuData = {};        // id -> { id, name_kr, price, category, ... }
const quantities = {};      // id -> number (regular menu items)
const rainbowQuantities = { // sub-item id -> number (rainbow road drinks)
    'rainbow-energy': 0,
    'rainbow-lemon': 0,
    'rainbow-greenapple': 0,
    'rainbow-orange': 0,
    'rainbow-sparkling': 0
};
const RAINBOW_PRICE = 1900; // each rainbow sub-item price
const RAINBOW_PARENT_ID = '12'; // id of 무지개로드 item
let cartExpanded = false;

// ============================================================
// Init
// ============================================================
document.addEventListener('DOMContentLoaded', function () {
    // Load menu data from hidden div
    const menuEl = document.getElementById('menu-data');
    if (menuEl && menuEl.dataset.menu) {
        try {
            const parsed = JSON.parse(menuEl.dataset.menu);
            Object.assign(menuData, parsed);
        } catch (e) {
            console.error('Failed to parse menu data:', e);
        }
    }

    // Init quantities for all menu items
    Object.keys(menuData).forEach(function (id) {
        quantities[id] = 0;
    });

    // Cart bar toggle
    const cartBarMain = document.getElementById('cartBarMain');
    if (cartBarMain) {
        cartBarMain.addEventListener('click', function (e) {
            // Don't toggle when clicking submit button
            if (e.target.closest('.cart-bar-submit')) return;
            toggleCartList();
        });
    }

    updateCartBar();
});

// ============================================================
// Quantity helpers
// ============================================================

/**
 * Find a menu item by ID.
 */
function findMenuItem(itemId) {
    return menuData[String(itemId)] || null;
}

/**
 * Update quantity for a regular menu item (shown as #quantity-{id}).
 */
function updateQuantity(itemId, delta) {
    const key = String(itemId);
    const current = quantities[key] || 0;
    const next = Math.max(0, current + delta);
    quantities[key] = next;

    // Update display
    const el = document.getElementById('quantity-' + key);
    if (el) {
        el.textContent = next;
    }

    updateCartBar();
}

/**
 * Update quantity for a Rainbow Road sub-item.
 */
function updateRainbowQty(subId, delta) {
    const current = rainbowQuantities[subId] || 0;
    const next = Math.max(0, current + delta);
    rainbowQuantities[subId] = next;

    // Update display
    const el = document.getElementById('qty-' + subId);
    if (el) {
        el.textContent = next;
    }

    updateCartBar();
}

// ============================================================
// Cart Bar
// ============================================================

/**
 * Toggle the expandable cart item list.
 */
function toggleCartList() {
    const listEl = document.getElementById('cartBarList');
    if (!listEl) return;
    cartExpanded = !cartExpanded;
    if (cartExpanded) {
        listEl.classList.add('expanded');
    } else {
        listEl.classList.remove('expanded');
    }
}

/**
 * Recalculate totals and update the fixed bottom cart bar.
 */
function updateCartBar() {
    const emptyText = document.getElementById('cartBarEmptyText');
    const filledDiv = document.getElementById('cartBarFilled');
    const totalEl = document.getElementById('cartBarTotal');
    const listInner = document.getElementById('cartBarListInner');
    const submitBtn = document.getElementById('cartBarSubmitBtn');

    // Calculate total from regular items
    let totalAmount = 0;
    const cartItems = []; // { id, name, qty, price }

    Object.keys(quantities).forEach(function (id) {
        const qty = quantities[id];
        if (qty > 0) {
            const item = findMenuItem(id);
            if (item) {
                const lineTotal = item.price * qty;
                totalAmount += lineTotal;
                cartItems.push({
                    id: id,
                    name: item.name_kr,
                    qty: qty,
                    price: lineTotal
                });
            }
        }
    });

    // Calculate total from rainbow sub-items
    let rainbowTotal = 0;
    let rainbowQty = 0;
    const rainbowItems = [];
    const rainbowNames = {
        'rainbow-energy': '에너지 드링크',
        'rainbow-lemon': '레몬',
        'rainbow-greenapple': '청사과',
        'rainbow-orange': '오렌지',
        'rainbow-sparkling': '탄산수'
    };

    Object.keys(rainbowQuantities).forEach(function (subId) {
        const qty = rainbowQuantities[subId];
        if (qty > 0) {
            const lineTotal = RAINBOW_PRICE * qty;
            rainbowTotal += lineTotal;
            rainbowQty += qty;
            rainbowItems.push({
                id: subId,
                name: rainbowNames[subId] || subId,
                qty: qty,
                price: lineTotal
            });
        }
    });

    totalAmount += rainbowTotal;

    const hasItems = totalAmount > 0;

    // Show/hide empty vs filled state
    if (emptyText) emptyText.style.display = hasItems ? 'none' : 'block';
    if (filledDiv) filledDiv.style.display = hasItems ? 'block' : 'none';

    // Update total
    if (totalEl) {
        totalEl.textContent = formatPrice(totalAmount) + '원';
    }

    // Enable/disable submit
    if (submitBtn) {
        submitBtn.disabled = !hasItems;
    }

    // Render cart list items
    if (listInner) {
        let html = '';

        // Regular items
        cartItems.forEach(function (ci) {
            html += '<div class="cart-list-item">';
            html += '  <span class="cart-list-item-name">' + escapeHtml(ci.name) + '</span>';
            html += '  <span class="cart-list-item-qty">' + ci.qty + '개</span>';
            html += '  <span class="cart-list-item-price">' + formatPrice(ci.price) + '원</span>';
            html += '</div>';
        });

        // Rainbow sub-items
        rainbowItems.forEach(function (ri) {
            html += '<div class="cart-list-item">';
            html += '  <span class="cart-list-item-name">🟣 ' + escapeHtml(ri.name) + '</span>';
            html += '  <span class="cart-list-item-qty">' + ri.qty + '개</span>';
            html += '  <span class="cart-list-item-price">' + formatPrice(ri.price) + '원</span>';
            html += '</div>';
        });

        listInner.innerHTML = html;
    }
}

// ============================================================
// Submit Order
// ============================================================

/**
 * Submit the order via POST /submit_order.
 * Sends all regular items and rainbow sub-items.
 */
function submitOrder() {
    const tableEl = document.getElementById('table-data');
    if (!tableEl) {
        alert('테이블 정보를 찾을 수 없습니다.');
        return;
    }
    const tableId = tableEl.dataset.tableId;

    // Build order menu dict: itemId -> quantity
    const orderMenu = {};

    // Regular items
    Object.keys(quantities).forEach(function (id) {
        const qty = quantities[id];
        if (qty > 0) {
            orderMenu[id] = qty;
        }
    });

    // Rainbow sub-items — use an existing drink item ID as proxy, or
    // send a special structure. Since the backend expects {item_id: qty},
    // we look up the actual drink item from menuData that maps to each flavor.
    // For now, if rainbow items exist, add to the 무지개로드 parent item's qty
    // as a single count, and include a notes field with the breakdown.
    if (rainbowQuantities) {
        let totalRainbowQty = 0;
        Object.keys(rainbowQuantities).forEach(function (subId) {
            totalRainbowQty += rainbowQuantities[subId];
        });

        if (totalRainbowQty > 0) {
            // Add rainbow items under the parent beverage item id
            // If item 12 doesn't exist in menuData, we find any drink item
            // The simplest approach: count rainbow drinks as qty of the parent
            const parentId = RAINBOW_PARENT_ID;
            if (menuData[parentId]) {
                orderMenu[parentId] = (orderMenu[parentId] || 0) + totalRainbowQty;
            } else {
                // Fallback: use the first drinks item as proxy
                const drinkItem = Object.keys(menuData).find(function (id) {
                    return menuData[id].category === 'drinks';
                });
                if (drinkItem) {
                    orderMenu[drinkItem] = (orderMenu[drinkItem] || 0) + totalRainbowQty;
                }
            }
        }
    }

    // Validate: must have at least one item
    const totalItems = Object.keys(orderMenu).length;
    if (totalItems === 0) {
        alert('주문할 메뉴를 선택해주세요.');
        return;
    }

    // Confirm
    const confirmMsg = '주문하시겠습니까?\n주문 후 취소가 불가할 수 있습니다.';
    if (!confirm(confirmMsg)) return;

    // Submit form
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/submit_order';

    const tableInput = document.createElement('input');
    tableInput.type = 'hidden';
    tableInput.name = 'table_id';
    tableInput.value = tableId;
    form.appendChild(tableInput);

    const menuInput = document.createElement('input');
    menuInput.type = 'hidden';
    menuInput.name = 'menu';
    menuInput.value = JSON.stringify(orderMenu);
    form.appendChild(menuInput);

    // Promo drink selection (optional, sent as note)
    const promoRadio = document.querySelector('input[name="promo-drink"]:checked');
    if (promoRadio) {
        const promoInput = document.createElement('input');
        promoInput.type = 'hidden';
        promoInput.name = 'promo_drink';
        promoInput.value = promoRadio.value;
        form.appendChild(promoInput);
    }

    // Rainbow breakdown as a hidden note
    const rainbowBreakdown = {};
    Object.keys(rainbowQuantities).forEach(function (subId) {
        if (rainbowQuantities[subId] > 0) {
            const rainbowNames = {
                'rainbow-energy': '에너지 드링크',
                'rainbow-lemon': '레몬',
                'rainbow-greenapple': '청사과',
                'rainbow-orange': '오렌지',
                'rainbow-sparkling': '탄산수'
            };
            rainbowBreakdown[subId] = {
                name: rainbowNames[subId],
                qty: rainbowQuantities[subId]
            };
        }
    });
    if (Object.keys(rainbowBreakdown).length > 0) {
        const rainbowInput = document.createElement('input');
        rainbowInput.type = 'hidden';
        rainbowInput.name = 'rainbow_breakdown';
        rainbowInput.value = JSON.stringify(rainbowBreakdown);
        form.appendChild(rainbowInput);
    }

    document.body.appendChild(form);
    form.submit();
}

// ============================================================
// Utility
// ============================================================

/**
 * Format number with commas.
 */
function formatPrice(num) {
    return Number(num).toLocaleString('ko-KR');
}

/**
 * Escape HTML to prevent XSS.
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
