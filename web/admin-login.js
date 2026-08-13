const form = document.querySelector("#adminLoginForm");
const errorBox = document.querySelector("#loginError");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.textContent = "";
  const button = form.querySelector("button");
  button.disabled = true;
  button.textContent = "正在验证…";
  try {
    const response = await fetch("/admin/api/login", {
      method: "POST", body: new FormData(form), credentials: "same-origin", cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "登录失败");
    location.replace("/admin");
  } catch (error) {
    errorBox.textContent = error.message || "登录失败，请重试";
    button.disabled = false;
    button.textContent = "登录管理后台";
  }
});
