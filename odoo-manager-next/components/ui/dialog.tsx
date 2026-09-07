import { Dialog as RadixDialog, IconButton } from "@radix-ui/themes";
import { X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/utils";

export const Dialog = RadixDialog.Root;
export const DialogTrigger = RadixDialog.Trigger;
export const DialogClose = RadixDialog.Close;

export const DialogContent = React.forwardRef<
  React.ElementRef<typeof RadixDialog.Content>,
  React.ComponentPropsWithoutRef<typeof RadixDialog.Content>
>(({ className, children, ...props }, ref) => (
  <RadixDialog.Content
    ref={ref}
    size="3"
    className={cn("relative max-h-[calc(100dvh-2rem-max(2rem,6dvh))] w-[calc(100vw-2rem)] max-w-lg overflow-y-auto", className)}
    {...props}
  >
    {children}
    <RadixDialog.Close>
      <IconButton
        className="absolute right-3 top-3"
        size="2"
        variant="ghost"
        color="gray"
        aria-label="Fermer"
        title="Fermer"
      >
        <X className="h-4 w-4" />
      </IconButton>
    </RadixDialog.Close>
  </RadixDialog.Content>
));
DialogContent.displayName = "DialogContent";

export function DialogHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1.5 pr-8", className)} {...props} />;
}

export const DialogTitle = React.forwardRef<
  React.ElementRef<typeof RadixDialog.Title>,
  React.ComponentPropsWithoutRef<typeof RadixDialog.Title>
>(({ className, ...props }, ref) => (
  <RadixDialog.Title ref={ref} className={cn("!mb-0 text-lg font-semibold", className)} {...props} />
));
DialogTitle.displayName = "DialogTitle";

export const DialogDescription = React.forwardRef<
  React.ElementRef<typeof RadixDialog.Description>,
  React.ComponentPropsWithoutRef<typeof RadixDialog.Description>
>(({ className, ...props }, ref) => (
  <RadixDialog.Description ref={ref} className={cn("text-sm text-muted-foreground", className)} {...props} />
));
DialogDescription.displayName = "DialogDescription";
