import { createRoot } from 'react-dom/client'
import { Button } from '@/generated/ui/button'
import { Dialog, DialogTrigger, DialogContent, DialogTitle, DialogDescription } from '@/generated/ui/dialog'
import '../src/theme.css'
createRoot(document.getElementById('root')!).render(
  <Dialog><DialogTrigger asChild><Button>Open fixture</Button></DialogTrigger><DialogContent><DialogTitle>Keyboard fixture</DialogTitle><DialogDescription>Temporary test content</DialogDescription><Button>First action</Button><Button>Last action</Button></DialogContent></Dialog>,
)
