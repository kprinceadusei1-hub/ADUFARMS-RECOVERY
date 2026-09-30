document.addEventListener("DOMContentLoaded", function () {
	const csrfMeta = document.querySelector('meta[name="csrf-token"]');
	if (csrfMeta) document.querySelectorAll("form[method='post'], form[method='POST']").forEach(function (form) {
		if (!form.querySelector('input[name="csrf_token"]')) {
			const csrfField = document.createElement("input");
			csrfField.type = "hidden";
			csrfField.name = "csrf_token";
			csrfField.value = csrfMeta.content;
			form.appendChild(csrfField);
		}
	});
	const passwordToggle = document.getElementById("passwordToggle");
	const passwordInput = document.getElementById("password");
	if (passwordToggle && passwordInput) passwordToggle.addEventListener("click", function () {
		const visible = passwordInput.type === "text";
		passwordInput.type = visible ? "password" : "text";
		passwordToggle.setAttribute("aria-pressed", String(!visible));
		passwordToggle.setAttribute("aria-label", visible ? "Show password" : "Hide password");
		const icon = passwordToggle.querySelector("i");
		if (icon) icon.className = visible ? "bi bi-eye" : "bi bi-eye-slash";
	});
	const loginForm = document.getElementById("loginForm");
	const loginSubmit = document.getElementById("loginSubmit");
	if (loginForm && loginSubmit) loginForm.addEventListener("submit", function () {
		if (loginSubmit.disabled) return;
		loginSubmit.disabled = true;
		loginSubmit.classList.add("is-loading");
	});
	const paymentCustomer = document.getElementById("paymentCustomer");
	const paymentSale = document.getElementById("paymentSale");
	if (paymentCustomer && paymentSale) {
		const paymentParams = new URLSearchParams(window.location.search);
		const requestedCustomer = paymentParams.get("customer_id") || "";
		const requestedSale = paymentParams.get("sales_id") || paymentParams.get("transaction_id") || "";
		const filterPaymentSales = function (preserveRequested) {
			const customer = String(paymentCustomer.value || "");
			let firstMatch = "";
			Array.from(paymentSale.options).forEach(function (option) {
				if (!option.value) return;
				const matches = String(option.dataset.customer || "") === customer;
				option.hidden = !matches;
				option.disabled = !matches;
				if (matches && !firstMatch) firstMatch = option.value;
			});
			paymentSale.disabled = !customer || !firstMatch;
			if (paymentSale.options[0]) paymentSale.options[0].textContent = customer
				? (firstMatch ? "Select outstanding sale" : "No outstanding sales for this customer")
				: "Select customer first";
			if (preserveRequested && requestedSale && firstMatch === requestedSale) paymentSale.value = requestedSale;
			else if (!preserveRequested) paymentSale.value = firstMatch || "";
		};
		paymentCustomer.addEventListener("change", function () { filterPaymentSales(false); });
		if (requestedCustomer) paymentCustomer.value = requestedCustomer;
		filterPaymentSales(Boolean(requestedCustomer));
	}
	const body = document.body;
	const sidebar = document.getElementById("appSidebar");
	const sidebarCollapse = document.getElementById("sidebarCollapse");
	const sidebarScrim = document.getElementById("sidebarScrim");
	const savedSidebar = localStorage.getItem("sidebarCollapsed") || localStorage.getItem("adufarms-sidebar");
	if (savedSidebar === "true" || savedSidebar === "collapsed") {
		if (window.matchMedia("(min-width: 901px)").matches) body.classList.add("sidebar-collapsed");
	}
	if (sidebarCollapse) sidebarCollapse.addEventListener("click", function () {
		if (window.matchMedia("(max-width: 900px)").matches) { body.classList.toggle("sidebar-open"); return; }
		body.classList.toggle("sidebar-collapsed");
		const collapsed = body.classList.contains("sidebar-collapsed");
		localStorage.setItem("sidebarCollapsed", String(collapsed));
		localStorage.setItem("adufarms-sidebar", collapsed ? "collapsed" : "expanded");
		const icon = sidebarCollapse.querySelector("i");
		if (icon) icon.className = body.classList.contains("sidebar-collapsed") ? "bi bi-layout-sidebar" : "bi bi-layout-sidebar-inset";
	});
	const railExpand = document.getElementById("railExpand");
	if (railExpand) railExpand.addEventListener("click", function () {
		body.classList.remove("sidebar-collapsed");
		try { localStorage.setItem("sidebarCollapsed", "false"); localStorage.setItem("adufarms-sidebar", "expanded"); } catch (e) {}
	});
	let railTip = null, railTipTimer = null;
	function hideRailTip() {
		if (railTipTimer) { window.clearTimeout(railTipTimer); railTipTimer = null; }
		if (railTip && railTip.parentNode) railTip.parentNode.removeChild(railTip);
		railTip = null;
	}
	document.querySelectorAll(".app-sidebar [data-tip]").forEach(function (el) {
		el.addEventListener("mouseenter", function () {
			if (!body.classList.contains("sidebar-collapsed")) return;
			hideRailTip();
			railTipTimer = window.setTimeout(function () {
				if (!body.classList.contains("sidebar-collapsed")) return;
				const rect = el.getBoundingClientRect();
				railTip = document.createElement("div");
				railTip.className = "rail-tip";
				railTip.textContent = el.getAttribute("data-tip");
				railTip.style.left = Math.round(rect.right + 12) + "px";
				railTip.style.top = Math.round(rect.top + rect.height / 2) + "px";
				railTip.style.transform = "translateY(-50%) scale(.96)";
				document.body.appendChild(railTip);
				requestAnimationFrame(function () {
					if (!railTip) return;
					railTip.style.transform = "translateY(-50%) scale(1)";
					railTip.classList.add("show");
				});
			}, 250);
		});
		el.addEventListener("mouseleave", hideRailTip);
		el.addEventListener("focus", function () {
			if (!body.classList.contains("sidebar-collapsed")) return;
			hideRailTip();
			const rect = el.getBoundingClientRect();
			railTip = document.createElement("div");
			railTip.className = "rail-tip show";
			railTip.textContent = el.getAttribute("data-tip");
			railTip.style.left = Math.round(rect.right + 12) + "px";
			railTip.style.top = Math.round(rect.top + rect.height / 2) + "px";
			railTip.style.transform = "translateY(-50%)";
		});
		el.addEventListener("blur", hideRailTip);
	});
	if (sidebar) sidebar.addEventListener("scroll", hideRailTip, { passive: true });
	const savedTheme = localStorage.getItem("adufarms-theme");
	if (savedTheme) body.dataset.theme = savedTheme;
	const themeToggle = document.getElementById("themeToggle");
	if (themeToggle) themeToggle.addEventListener("click", function () {
		const nextTheme = body.dataset.theme === "dark" ? "light" : "dark";
		body.dataset.theme = nextTheme;
		localStorage.setItem("adufarms-theme", nextTheme);
		const icon = themeToggle.querySelector("i");
		if (icon) icon.className = nextTheme === "dark" ? "bi bi-sun" : "bi bi-moon-stars";
	});
	const menuToggle = document.getElementById("mobileMenuToggle");
	if (menuToggle) menuToggle.addEventListener("click", function () {
		if (window.matchMedia("(max-width: 900px)").matches) { body.classList.toggle("sidebar-open"); return; }
		body.classList.remove("sidebar-collapsed");
		try { localStorage.setItem("sidebarCollapsed", "false"); localStorage.setItem("adufarms-sidebar", "expanded"); } catch (e) {}
	});
	if (sidebarScrim) sidebarScrim.addEventListener("click", function () { body.classList.remove("sidebar-open"); });
	if (sidebar) sidebar.querySelectorAll(".sidebar-nav a").forEach(function (link) {
		if (!link.getAttribute("aria-label")) {
			const label = link.querySelector("span:not(.nav-icon)");
			if (label) link.setAttribute("aria-label", label.textContent.trim());
		}
	});
	document.querySelectorAll(".app-sidebar a").forEach(function (link) { link.addEventListener("click", function () { body.classList.remove("sidebar-open"); }); });
	const popoverPairs = [["notificationToggle", "notificationPanel"], ["profileToggle", "profilePanel"], ["newTransactionToggle", "newTransactionPanel"], ["sidebarProfileToggle", "sidebarProfilePanel"]];
	const closePopovers = function () {
		document.querySelectorAll(".popover-panel.open, .sidebar-popover.open").forEach(function (panel) {
			panel.classList.remove("open");
			const toggle = document.querySelector('[aria-controls="' + panel.id + '"]');
			if (toggle) toggle.setAttribute("aria-expanded", "false");
		});
	};
	popoverPairs.forEach(function (pair) {
		const toggle = document.getElementById(pair[0]);
		const panel = document.getElementById(pair[1]);
		if (!toggle || !panel) return;
		toggle.addEventListener("click", function (event) {
			event.stopPropagation();
			closePopovers();
			panel.classList.toggle("open");
			toggle.setAttribute("aria-expanded", panel.classList.contains("open"));
		});
		panel.addEventListener("click", function (event) { event.stopPropagation(); });
	});
	document.addEventListener("click", closePopovers);
	document.addEventListener("keydown", function (event) { if (event.key === "Escape") { body.classList.remove("sidebar-open"); closePopovers(); } });

	const modalElement = document.getElementById("actionConfirmModal");
	if (modalElement && typeof bootstrap !== "undefined") {
		const modal = new bootstrap.Modal(modalElement);
		const message = document.getElementById("actionConfirmMessage");
		const notice = modalElement.querySelector(".modal-body .alert");
		const confirmButton = document.getElementById("actionConfirmButton");
		const title = modalElement.querySelector(".modal-title");
		const reasonLabel = modalElement.querySelector("label[for='reversalReason']");
		const reversalReason = document.getElementById("reversalReason");
		let pendingForm = null;
		document.querySelectorAll("form[onsubmit]").forEach(function (form) {
			const inlineHandler = form.getAttribute("onsubmit") || "";
			if (inlineHandler.includes("confirm")) {
				form.removeAttribute("onsubmit");
				form.dataset.confirm = form.action.includes("/restore") ? "Restore this transaction?" : "Are you sure you want to reverse this transaction?";
			}
		});
		document.querySelectorAll("form[data-confirm]").forEach(function (form) {
			form.addEventListener("submit", function (event) {
				if (form.dataset.confirmed === "true") { form.dataset.confirmed = "false"; return; }
				event.preventDefault();
				pendingForm = form;
				const isRestore = form.action.includes("/restore");
				if (title) title.textContent = isRestore ? "Confirm Transaction Restoration" : "Confirm Transaction Reversal";
				if (notice) notice.innerHTML = '<i class="bi bi-exclamation-triangle-fill me-2" aria-hidden="true"></i>' + (isRestore ? "This restoration is recorded in the audit log and returns the transaction to active records." : "This reversal is recorded in the audit log and does not permanently delete the transaction.");
				if (message) { message.textContent = ""; message.classList.add("d-none"); }
				if (reasonLabel) reasonLabel.innerHTML = (isRestore ? "Business reason for restoration" : "Business reason for reversal") + ' <span class="required-mark">*</span>';
				if (confirmButton) { confirmButton.textContent = isRestore ? "Restore Transaction" : "Reverse Transaction"; confirmButton.className = "btn " + (isRestore ? "btn-success" : "btn-warning"); }
				if (reversalReason) { reversalReason.value = ""; reversalReason.placeholder = isRestore ? "Example: Reversal was made in error" : "Example: Duplicate or incorrect transaction"; reversalReason.classList.remove("is-invalid"); }
				modal.show();
			});
		});
		if (confirmButton) confirmButton.addEventListener("click", function () {
			if (!pendingForm) return;
			const reason = reversalReason ? reversalReason.value.trim() : "";
			if (!reason) { if (reversalReason) { reversalReason.classList.add("is-invalid"); reversalReason.focus(); } return; }
			let field = pendingForm.querySelector('input[name="reason"]');
			if (!field) { field = document.createElement("input"); field.type = "hidden"; field.name = "reason"; pendingForm.appendChild(field); }
			field.value = reason; pendingForm.dataset.confirmed = "true"; modal.hide(); pendingForm.requestSubmit(); pendingForm = null;
		});
	}
	const deleteModalElement = document.getElementById("deleteTransactionModal");
	const deleteForm = document.getElementById("deleteTransactionForm");
	if (deleteModalElement && deleteForm) {
		deleteModalElement.addEventListener("show.bs.modal", function (event) {
			const trigger = event.relatedTarget;
			if (!trigger) return;
			deleteForm.action = trigger.dataset.deleteUrl;
			const type = trigger.dataset.deleteType || "Transaction";
			const rowCells = trigger.closest("tr") ? trigger.closest("tr").children : [];
			const cellText = function (index) { return rowCells[index] ? rowCells[index].textContent.trim() : "-"; };
			const title = document.getElementById("deleteTransactionTitle");
			const messageText = document.getElementById("deleteTransactionMessage");
			const submitText = document.getElementById("deleteTransactionSubmit");
			if (title) title.textContent = "Permanently Delete " + type;
			if (messageText) messageText.textContent = "This permanently deletes the selected " + type.toLowerCase() + " and cannot be undone. Your reason will be retained in the audit log.";
			if (submitText) submitText.textContent = "Permanently Delete " + type;
			const fields = { Id: "deleteId", Type: "deleteType", Party: "deleteParty", Quantity: "deleteQuantity", Amount: "deleteAmount", Date: "deleteDate" };
			Object.keys(fields).forEach(function (field) {
				const target = document.getElementById("deleteTransaction" + field);
				if (target) target.textContent = trigger.dataset[fields[field]] || "-";
			});
			const paid = document.getElementById("deleteTransactionPaid");
			const balance = document.getElementById("deleteTransactionBalance");
			const method = document.getElementById("deleteTransactionMethod");
			if (type === "Sale") {
				if (paid) paid.textContent = cellText(6);
				if (balance) balance.textContent = cellText(7);
				if (method) method.textContent = "-";
			} else if (type === "Payment") {
				if (paid) paid.textContent = "-";
				if (balance) balance.textContent = "-";
				if (method) method.textContent = cellText(4);
			} else {
				if (paid) paid.textContent = "-";
				if (balance) balance.textContent = "-";
				if (method) method.textContent = "-";
			}
			const reason = document.getElementById("deleteReason");
			if (reason) { reason.value = ""; reason.classList.remove("is-invalid"); }
		});
		deleteForm.addEventListener("submit", function (event) {
			const reason = document.getElementById("deleteReason");
			if (!reason || !reason.value.trim()) { event.preventDefault(); reason.classList.add("is-invalid"); reason.focus(); }
		});
	}
	document.querySelectorAll("form").forEach(function (form) {
		form.addEventListener("submit", function () {
			if (form.id === "loginForm") return;
			const button = form.querySelector("button[type='submit'], button:not([type])");
			if (!button || form.dataset.confirmed === "true") return;
			button.disabled = true;
			button.dataset.originalText = button.innerHTML;
			button.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span> Processing...';
		});
	});
	document.querySelectorAll(".app-alerts .alert").forEach(function (alert) {
		let remaining = 5000, start = Date.now(), timer = null;
		function closeAlert() { if (typeof bootstrap !== "undefined") bootstrap.Alert.getOrCreateInstance(alert).close(); }
		function schedule(delay) { timer = window.setTimeout(closeAlert, delay); }
		schedule(remaining);
		alert.addEventListener("mouseenter", function () { if (timer) { window.clearTimeout(timer); timer = null; remaining -= Date.now() - start; } });
		alert.addEventListener("mouseleave", function () { if (!timer) { start = Date.now(); schedule(Math.max(remaining, 1500)); } });
		alert.addEventListener("focusin", function () { if (timer) { window.clearTimeout(timer); timer = null; remaining -= Date.now() - start; } });
		alert.addEventListener("focusout", function () { if (!timer) { start = Date.now(); schedule(Math.max(remaining, 1500)); } });
	});
	document.querySelectorAll("[data-table-search]").forEach(function (input) {
		input.addEventListener("input", function () {
			const table = document.querySelector(input.dataset.tableSearch);
			if (!table) return;
			const query = input.value.toLowerCase();
			table.querySelectorAll("tbody tr").forEach(function (row) {
				row.hidden = query && !row.textContent.toLowerCase().includes(query);
			});
			const card = table.closest(".card");
			if (card) paginateCard(card);
		});
	});
	document.querySelectorAll("table.sortable thead th[data-sort]").forEach(function (th) {
		th.style.cursor = "pointer";
		th.title = "Sort";
		th.setAttribute("tabindex", "0");
		th.setAttribute("role", "columnheader");
		if (!th.hasAttribute("aria-sort")) th.setAttribute("aria-sort", "none");
		function activateSort() { th.click(); }
		th.addEventListener("click", function () {
			const table = th.closest("table");
			const idx = Array.prototype.indexOf.call(th.parentNode.children, th);
			const asc = th.dataset.dir !== "asc";
			table.querySelectorAll("thead th").forEach(function (h) { delete h.dataset.dir; h.setAttribute("aria-sort", "none"); });
			th.dataset.dir = asc ? "asc" : "desc";
			th.setAttribute("aria-sort", asc ? "ascending" : "descending");
			const rows = Array.from(table.querySelectorAll("tbody tr"));
			rows.sort(function (a, b) {
				const av = (a.children[idx] ? a.children[idx].textContent.trim() : "").toLowerCase();
				const bv = (b.children[idx] ? b.children[idx].textContent.trim() : "").toLowerCase();
				const an = parseFloat(av.replace(/[^0-9.\-]/g, ""));
				const bn = parseFloat(bv.replace(/[^0-9.\-]/g, ""));
				let cmp = 0;
				if (!isNaN(an) && !isNaN(bn) && av.match(/[0-9]/) && bv.match(/[0-9]/)) cmp = an - bn;
				else cmp = av.localeCompare(bv);
				return asc ? cmp : -cmp;
			});
			const tb = table.querySelector("tbody");
			rows.forEach(function (r) { tb.appendChild(r); });
		});
		th.addEventListener("keydown", function (e) {
			if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activateSort(); }
		});
	});
	window.paginateCard = paginateCard;
	function paginateCard(card) {
		const table = card.querySelector("table");
		if (!table) return;
		let page = parseInt(card.dataset.page || "1", 10);
		const per = 15;
		const rows = Array.from(table.querySelectorAll("tbody tr")).filter(function (r) { return !r.hidden && !r.querySelector("td[colspan]"); });
		const pages = Math.max(1, Math.ceil(rows.length / per));
		if (page > pages) page = pages;
		card.dataset.page = String(page);
		rows.forEach(function (r, i) {
			r.style.display = (i >= (page - 1) * per && i < page * per) ? "" : "none";
		});
		const info = card.querySelector("[data-page-info]");
		if (info) info.textContent = rows.length ? "Page " + page + " of " + pages + " · " + rows.length + (rows.length === 1 ? " row" : " rows") : "No records to show";
	}
	document.querySelectorAll(".card").forEach(function (card) {
		if (!card.querySelector("table") || !card.querySelector("[data-page-next]")) return;
		card.dataset.page = "1";
		paginateCard(card);
		card.querySelector("[data-page-next]").addEventListener("click", function () { card.dataset.page = String(parseInt(card.dataset.page, 10) + 1); paginateCard(card); });
		card.querySelector("[data-page-prev]").addEventListener("click", function () { card.dataset.page = String(Math.max(1, parseInt(card.dataset.page, 10) - 1)); paginateCard(card); });
	});

	// Pin the Actions column of wide ledger tables so buttons stay reachable without sideways scrolling.
	document.querySelectorAll(".table-responsive > table").forEach(function (table) {
		var firstRow = table.querySelector("tbody tr");
		var lastCell = firstRow && firstRow.lastElementChild;
		var headCell = table.querySelector("thead tr:last-child th:last-child");
		if (!lastCell || !headCell || !lastCell.querySelector(".btn, button, form")) { return; }
		table.classList.add("has-sticky-actions");
		headCell.classList.add("sticky-actions");
		table.querySelectorAll("tbody tr").forEach(function (row) {
			if (row.lastElementChild && row.children.length > 2) { row.lastElementChild.classList.add("sticky-actions"); }
		});
	});

	// Label every cell with its column header so tables can reflow into cards on phones (see .table-cards in CSS).
	document.querySelectorAll(".table-responsive > table").forEach(function (table) {
		var heads = Array.from(table.querySelectorAll("thead tr:last-child th")).map(function (th) {
			return th.textContent.replace(/[↕↑↓]/g, "").trim();
		});
		if (!heads.length) { return; }
		table.classList.add("table-cards");
		table.querySelectorAll("tbody tr").forEach(function (row) {
			Array.from(row.children).forEach(function (cell, index) {
				if (cell.hasAttribute("colspan")) { return; }
				cell.setAttribute("data-label", heads[index] || "");
				if (!heads[index] || heads[index] === "#") { cell.classList.add("card-cell-bare"); }
			});
		});
	});
});
