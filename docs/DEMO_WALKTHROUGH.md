# Text-to-SQL System Demo Walkthrough

## 🚀 Overview
This document demonstrates the **Hybrid Agentic Text-to-SQL System** in action. The system successfully processes natural language questions, generates accurate SQL using specialized models, and visualizes the results.

The application does not ship with or automatically load a sample database.
Add and select a database from the data catalog before using the workflow below;
table names and results depend on that selected source.

---

## 1. The Interface
The application features a clean, professional interface built with Streamlit.
- **Left Sidebar**: System status, architecture details, and example questions.
- **Main Area**: Chat input and results display.

![App Interface](images/interface.png)

### Key Features Visible:
- **Natural Language Input**: A question about a table in the selected database
- **Status Indicator**: "SUCCESS: Found 1 result for your question."
- **Chain-of-Thought**: Collapsible section showing the agent's reasoning process.

---

## 2. Results & Visualization
The system displays results in a side-by-side layout for maximum clarity.

![Results Display](images/results.png)

### 🔍 Analysis of the Output:

1.  **Generated SQL** (illustrative):
    ```sql
    SELECT COUNT(*) FROM albums LIMIT 50;
    ```
    *Note: This historical example assumes that the selected database contains
    an `albums` table. The system also applies the `LIMIT 50` guardrail.*

2.  **Data Table (Left)**:
    - Shows the raw result: `347`.
    - The displayed value comes from the database selected by the user.

3.  **Visualization (Right)**:
    - **Intelligent Charting**: The system automatically selected a **Bar Chart** to visualize the single count value.
    - **Title**: "Number of Albums" (Auto-generated).
    - **Interactive**: The chart is interactive (powered by Plotly).

---

## 🧠 Configured LLM Workflow in Action

Although invisible to the user, this query triggered the following workflow:

1.  **Intent Router (configured model)**: Classified the question as "Relevant".
2.  **SQL Generator (configured model)**:
    - Received the schema for `albums`.
    - Generated the precise SQL query.
3.  **SQL Validator**: Checked for safety (no DROP/DELETE) and syntax.
4.  **Visualizer (configured model)**: Decided a Bar Chart was the best way to show a "Count".

---

## ✅ Conclusion
The system demonstrates **Production-Grade** capabilities:
- **Accuracy**: Correct SQL generation.
- **Safety**: Automatic limit enforcement.
- **UX**: Instant visualization without user configuration.
