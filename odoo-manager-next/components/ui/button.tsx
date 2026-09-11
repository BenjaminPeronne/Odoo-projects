import { Button as RadixButton } from "@radix-ui/themes";
import * as React from "react";
import { cn } from "@/lib/utils";

type ButtonVariant = "default" | "secondary" | "outline" | "ghost" | "destructive";
type ButtonSize = "default" | "sm" | "icon";

const buttonVariants = ({ variant = "default", size = "default", className }: { variant?: ButtonVariant | null; size?: ButtonSize | null; className?: string } = {}) =>
  cn(
    "box-border min-w-0 select-none justify-center gap-2 whitespace-normal text-center font-medium leading-snug transition-[background-color,border-color,box-shadow,filter,transform] duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:!border-border disabled:!bg-muted disabled:!text-muted-foreground disabled:!shadow-none disabled:!filter-none disabled:opacity-60 [&_svg]:shrink-0",
    (variant === "default" || variant === "destructive") && "app-solid-button hover:brightness-[0.88] active:brightness-[0.93]",
    variant === "default" && "app-primary-button",
    variant === "secondary" && "hover:bg-secondary/80 active:bg-secondary/70",
    variant === "outline" && "hover:border-primary/45 hover:bg-primary/[0.12] active:bg-primary/[0.10] dark:hover:bg-primary/[0.12]",
    variant === "ghost" && "m-0 hover:bg-muted active:bg-muted/80",
    variant === "destructive" && "app-destructive-button",
    size === "default" && "h-auto min-h-10 px-4 py-2",
    size === "sm" && "h-auto min-h-9 px-3 py-1.5",
    size === "icon" && "h-10 w-10 p-0",
    className,
  );

export interface ButtonProps
  extends Omit<React.ComponentPropsWithoutRef<typeof RadixButton>, "color" | "size" | "variant"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

const Button = React.forwardRef<React.ElementRef<typeof RadixButton>, ButtonProps>(
  ({ className, variant = "default", size = "default", ...props }, ref) => {
    const radixVariant =
      variant === "secondary" ? "soft" : variant === "outline" ? "outline" : variant === "ghost" ? "ghost" : "solid";
    const color = variant === "destructive" ? "red" : variant === "secondary" || variant === "outline" || variant === "ghost" ? "gray" : "orange";
    const radixSize = size === "sm" || size === "icon" ? "2" : "3";

    return (
      <RadixButton
        ref={ref}
        variant={radixVariant}
        color={color}
        size={radixSize}
        className={buttonVariants({ variant, size, className })}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
