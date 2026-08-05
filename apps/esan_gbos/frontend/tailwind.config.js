import frappeUIPreset from "frappe-ui/tailwind";

export default {
  presets: [frappeUIPreset],
  content: [
    "./index.html",
    "./src/**/*.{vue,ts}",
    "./node_modules/frappe-ui/src/components/Button/**/*.{vue,ts}",
    "./node_modules/frappe-ui/src/components/LoadingIndicator.vue",
    "./node_modules/frappe-ui/src/components/FeatherIcon.vue",
    "./node_modules/frappe-ui/src/components/Tooltip/**/*.{vue,ts}",
  ],
};
