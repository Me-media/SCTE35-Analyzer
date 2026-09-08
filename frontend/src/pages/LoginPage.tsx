import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import logo from "../assets/logo.png";
import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../api/client";
import { PasswordField } from "../components/PasswordField";

export function LoginPage() {
  const { t } = useTranslation();
  const { login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(username, password);
      navigate("/channels");
    } catch (err) {
      if (err instanceof ApiError) {
        // 401 = wrong username/password → generic message, same as before.
        // 403 now also covers pending-approval / rejected accounts and
        // IP-blocked attempts (spec: new brute-force protection + the
        // approval workflow) — those have a real, distinct reason the
        // person should see rather than being told their password is wrong.
        setError(err.status === 401 ? t("auth.invalidCredentials") : err.message);
      } else {
        setError(String(err));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-dark px-4">
      <form onSubmit={onSubmit} className="w-full max-w-sm rounded-lg border border-white/10 bg-surface-dark-raised p-8">
        <img src={logo} alt={t("app.name")} className="mx-auto mb-8 h-10 w-auto" />
        <label className="mb-1 block text-sm text-gray-300">{t("auth.username")}</label>
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
          required
          className="mb-4 w-full rounded border border-white/10 bg-surface-dark px-3 py-2 text-gray-100"
        />
        <label className="mb-1 block text-sm text-gray-300">{t("auth.password")}</label>
        <PasswordField
          value={password}
          onChange={setPassword}
          required
          className="mb-2 w-full rounded border border-white/10 bg-surface-dark px-3 py-2 text-gray-100"
          toggleColorClassName="text-gray-500 hover:text-gray-300"
        />
        <p className="mb-4 text-xs text-gray-500">{t("auth.breakGlassHint")}</p>
        {error && <p className="mb-4 text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded bg-gold-500 py-2 font-medium text-black hover:bg-gold-400 disabled:opacity-50"
        >
          {t("auth.signIn")}
        </button>
      </form>
    </div>
  );
}
