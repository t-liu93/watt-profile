import { useRef, useState } from 'react'
import type { ComponentProps, ReactNode } from 'react'
import { Button as RadixButton } from '@/generated/ui/button'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/generated/ui/dialog'
export function Button({ className = '', children, onClick, ...props }: ComponentProps<typeof RadixButton>) {
  const [pulse, setPulse] = useState(0)
  return <RadixButton type="button" className={`neon-button ${className}`} {...props} onClick={event => { setPulse(value => value + 1); onClick?.(event) }}><span>{children}</span><span key={pulse} className={`ripple ${pulse ? 'ripple-active' : ''}`} aria-hidden="true" /></RadixButton>
}
export function NavigationDialog({ title, closeLabel, children }: { title: string, closeLabel: string, children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button className="mobile-menu">{title}</Button></DialogTrigger><DialogContent className="navigation-dialog"><DialogTitle>{title}</DialogTitle><DialogDescription className="sr-only">Watt Profile</DialogDescription><div onClick={event => { if ((event.target as HTMLElement).closest('a')) setOpen(false) }}>{children}</div><DialogClose asChild><Button>{closeLabel}</Button></DialogClose></DialogContent></Dialog>
}
export function ConfirmationDialog({ open, title, description, confirmLabel, cancelLabel, busy, onCancel, onConfirm }: { open: boolean; title: string; description: string; confirmLabel: string; cancelLabel: string; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const returnFocus = useRef<HTMLElement | null>(null)
  return <Dialog open={open} onOpenChange={value => { if (!value && !busy) onCancel() }}><DialogContent className="confirmation-dialog" onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus() }} onOpenAutoFocus={event => { returnFocus.current = document.activeElement as HTMLElement; event.preventDefault(); (event.currentTarget as HTMLElement).querySelector<HTMLButtonElement>('[data-cancel]')?.focus() }}><DialogTitle>{title}</DialogTitle><DialogDescription>{description}</DialogDescription><div className="action-row"><Button data-cancel disabled={busy} onClick={onCancel}>{cancelLabel}</Button><Button disabled={busy} onClick={onConfirm}>{confirmLabel}</Button></div></DialogContent></Dialog>
}
