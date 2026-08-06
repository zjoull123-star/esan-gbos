declare module "@frappe-ui/button" {
  import type { DefineComponent } from "vue";

  const Button: DefineComponent<{
    theme?: "gray" | "blue" | "green" | "red";
    variant?: "solid" | "subtle" | "outline" | "ghost";
    type?: "button" | "submit" | "reset";
    loading?: boolean;
    disabled?: boolean;
  }>;

  export default Button;
}
