/**
 * DeleteConfirmDialog — asks the user to confirm before deleting files.
 *
 * Deletion moves items to the app-trash folder and is fully undoable.
 * The dialog describes this so users are not alarmed by "Delete".
 */
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from './ui/alert-dialog';

interface DeleteConfirmDialogProps {
  open: boolean;
  paths: string[];
  onConfirm: () => void;
  onCancel: () => void;
}

export function DeleteConfirmDialog({
  open,
  paths,
  onConfirm,
  onCancel,
}: DeleteConfirmDialogProps) {
  const count = paths.length;
  const label = count === 1 ? '1 item' : `${count} items`;

  return (
    <AlertDialog open={open} onOpenChange={(v) => !v && onCancel()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete {label}?</AlertDialogTitle>
          <AlertDialogDescription>
            {label} will be moved to the app trash. You can undo this action with{' '}
            <kbd className="px-1 py-0.5 text-xs font-mono bg-gray-100 border rounded">Ctrl+Z</kbd>.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={onCancel}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            className="bg-red-600 hover:bg-red-700 focus:ring-red-600"
          >
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
