import { useId, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";

interface PasswordFieldProps {
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  minLength?: number;
  maxLength?: number;
  placeholder?: string;
  id?: string;
  autoComplete?: string;
  // Extra classes/style for the <input> itself - merged onto this
  // component's own base classes (which always include right padding for
  // the toggle button, so callers don't need to account for that).
  className?: string;
  style?: CSSProperties;
  // Most pages theme inputs via CSS custom properties (var(--text-muted),
  // see globals.css) and can leave this unset. LoginPage.tsx is
  // permanently dark regardless of the app's light/dark/system setting
  // (see Header.tsx's own docstring for the same reasoning) and uses
  // fixed Tailwind gray classes instead - pass a color class here for
  // that case so the toggle icon matches instead of picking an
  // invisible-on-dark CSS-var color.
  toggleColorClassName?: string;
}

// Plain inline SVGs, not an icon library - this project has none installed
// and no package-registry access in some environments, so a dependency-free
// icon is the reliable choice here (matches StatChart.tsx's existing raw
// <svg> precedent elsewhere in this codebase).
function EyeIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" className="h-4 w-4">
      <path d="M1.5 12S5.5 4.5 12 4.5 22.5 12 22.5 12 18.5 19.5 12 19.5 1.5 12 1.5 12Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" className="h-4 w-4">
      <path d="M3 3l18 18" />
      <path d="M10.58 10.59a2 2 0 0 0 2.83 2.83" />
      <path d="M9.88 4.76A9.77 9.77 0 0 1 12 4.5c6.5 0 10.5 7.5 10.5 7.5a13.16 13.16 0 0 1-3.14 3.9M6.6 6.61C3.55 8.36 1.5 12 1.5 12s4 7.5 10.5 7.5a9.9 9.9 0 0 0 4.9-1.32" />
    </svg>
  );
}

// Spec (Mattias): an eye icon in the field that toggles showing/hiding a
// password after it's been typed - covers both the login password
// (auth.password) and every SRT passphrase field (channel.auth.passphrase,
// input and output legs both use the same TransportSection component in
// ChannelFormPage.tsx). One shared component rather than duplicating the
// toggle state/markup at each of the three call sites.
export function PasswordField({
  value, onChange, required, minLength, maxLength, placeholder, id, autoComplete, className, style, toggleColorClassName,
}: PasswordFieldProps) {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(false);
  const generatedId = useId();
  const inputId = id ?? generatedId;

  return (
    <div className="relative">
      <input
        id={inputId}
        type={visible ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required={required}
        minLength={minLength}
        maxLength={maxLength}
        placeholder={placeholder}
        autoComplete={autoComplete}
        className={`pr-9 ${className ?? ""}`}
        style={style}
      />
      <button
        type="button"
        // Never part of the form's tab order - it toggles a display
        // preference, not a field to fill in, and would otherwise land
        // awkwardly between the passphrase and the next real field.
        tabIndex={-1}
        onClick={() => setVisible((v) => !v)}
        aria-label={visible ? t("common.hidePassword") : t("common.showPassword")}
        aria-pressed={visible}
        className={`absolute inset-y-0 right-0 flex items-center px-2 ${toggleColorClassName ?? ""}`}
        style={toggleColorClassName ? undefined : { color: "var(--text-muted)" }}
      >
        {visible ? <EyeOffIcon /> : <EyeIcon />}
      </button>
    </div>
  );
}
