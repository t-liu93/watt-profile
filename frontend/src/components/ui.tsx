import { useState } from 'react'
import type { ComponentProps, ReactNode } from 'react'
import { Button as RadixButton } from '@/generated/ui/button'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/generated/ui/dialog'
export function Button({ className = '', children, onClick, ...props }: ComponentProps<typeof RadixButton>) {
  const [pulse, setPulse] = useState(0)
  return <RadixButton className={`neon-button ${className}`} {...props} onClick={event => { setPulse(value => value + 1); onClick?.(event) }}><span>{children}</span><span key={pulse} className={`ripple ${pulse ? 'ripple-active' : ''}`} aria-hidden="true" /></RadixButton>
}
export function NavigationDialog({ title, closeLabel, children }: { title: string, closeLabel: string, children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button className="mobile-menu">{title}</Button></DialogTrigger><DialogContent className="navigation-dialog"><DialogTitle>{title}</DialogTitle><DialogDescription className="sr-only">Watt Profile</DialogDescription><div onClick={event => { if ((event.target as HTMLElement).closest('a')) setOpen(false) }}>{children}</div><DialogClose asChild><Button>{closeLabel}</Button></DialogClose></DialogContent></Dialog>
}
