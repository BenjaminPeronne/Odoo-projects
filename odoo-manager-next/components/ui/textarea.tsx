import { TextArea as RadixTextArea } from "@radix-ui/themes";
import * as React from "react";
import { cn } from "@/lib/utils";

export const Textarea = React.forwardRef<
  React.ElementRef<typeof RadixTextArea>,
  React.ComponentPropsWithoutRef<typeof RadixTextArea>
>(({ className, ...props }, ref) => (
  <RadixTextArea ref={ref} size="3" variant="surface" resize="vertical" className={cn("min-h-24 w-full", className)} {...props} />
));
Textarea.displayName = "Textarea";
