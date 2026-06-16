import { beginMicrosoftLogin } from '@/lib/microsoft-login'

export function SignInButton() {
  return (
    <button
      type="button"
      onClick={beginMicrosoftLogin}
      className="inline-flex items-center gap-2.5 rounded-lg bg-brand px-5 py-2.5 font-medium text-white shadow-sm transition-colors hover:bg-brand-hover"
    >
      <MicrosoftLogo />
      Sign in with Microsoft
    </button>
  )
}

function MicrosoftLogo() {
  return (
    <svg viewBox="0 0 21 21" className="h-4 w-4" aria-hidden="true">
      <rect width="10" height="10" fill="#f25022" />
      <rect x="11" width="10" height="10" fill="#7fba00" />
      <rect y="11" width="10" height="10" fill="#00a4ef" />
      <rect x="11" y="11" width="10" height="10" fill="#ffb900" />
    </svg>
  )
}
