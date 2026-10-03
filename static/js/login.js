/* Sign-in page behaviour: time-aware greeting, Caps Lock hint, photo parallax.
   Purely cosmetic; the form itself is handled by app.js and the server. */
(function () {
	"use strict";

	var greeting = document.getElementById("loginGreeting");
	if (greeting) {
		var hour = new Date().getHours();
		var part = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
		greeting.textContent = part;
	}

	var password = document.getElementById("password");
	var caps = document.getElementById("capsHint");
	if (password && caps) {
		var update = function (event) {
			if (!event.getModifierState) return;
			caps.hidden = !event.getModifierState("CapsLock");
		};
		password.addEventListener("keydown", update);
		password.addEventListener("keyup", update);
		password.addEventListener("blur", function () { caps.hidden = true; });
	}

	var visual = document.querySelector(".login-visual");
	var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
	if (visual && !reduced && window.matchMedia("(min-width: 701px)").matches) {
		var frame = 0;
		window.addEventListener("mousemove", function (event) {
			if (frame) return;
			frame = window.requestAnimationFrame(function () {
				frame = 0;
				var x = (event.clientX / window.innerWidth - 0.5) * 2;
				var y = (event.clientY / window.innerHeight - 0.5) * 2;
				visual.style.setProperty("--px", (x * -14).toFixed(1) + "px");
				visual.style.setProperty("--py", (y * -10).toFixed(1) + "px");
			});
		}, { passive: true });
	}
})();
