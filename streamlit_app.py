import streamlit as st
import xcelgrad_sales
import xcelgrad_tech

st.set_page_config(page_title="Resume Parsing Toolkit", layout="wide")

def main():
    st.title("📂 Resume Parsing Toolkit")

    mode = st.sidebar.radio(
        "Choose Tool",
        ["Skills from Experience (Tech Stack)", "Industry / Vertical Mapping"],
        index=0
    )

    if mode == "Skills from Experience (Tech Stack)":
        xcelgrad_sales.main()
    else:
        xcelgrad_tech.main()

if __name__ == "__main__":
    main()
