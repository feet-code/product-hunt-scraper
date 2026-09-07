"""Bounded semantic vocabulary: one vector per intent, never per product."""
LABELS = (
    "Invoice operations", "Accounting and bookkeeping", "Payments and billing",
    "Financial planning", "Trading and investment research", "Crypto analytics",
    "Sales prospecting", "Customer relationship management", "Email marketing",
    "Social media management", "Search engine optimization", "Advertising optimization",
    "Marketing analytics", "Customer support", "Reviews and reputation",
    "Customer onboarding", "Product analytics", "User research", "Surveys and forms",
    "Project management", "Team collaboration", "Meetings and scheduling",
    "Document workflows", "Knowledge management", "Workflow automation",
    "Recruiting and careers", "People operations", "Training and education",
    "Developer tools", "Software testing", "Application monitoring", "Cybersecurity",
    "Identity and access", "Data integration", "Data analysis", "Database operations",
    "Website and app building", "Ecommerce operations", "Inventory and procurement",
    "Shipping and logistics", "Vendor management", "Property management",
    "Architecture and rendering", "Design and image editing", "Video creation",
    "Audio and music creation", "Writing and publishing", "Community management",
    "Events and media sharing", "Healthcare workflows", "Fitness and wellness",
    "Legal workflows", "Compliance management", "Travel planning",
    "Agriculture operations", "Local business operations", "Personal productivity",
    "Gaming and entertainment", "General software utilities",
)
INTENTS = {
    label.lower().replace(" ", "-"): {
        "key": label.lower().replace(" ", "-"), "label": label,
        "searchText": f"{label}. Software for {label.lower()} tasks, workflows, and analysis.",
    } for label in LABELS
}
