import streamlit as st

import epics



# Title of the app

st.title("EPICS Monitoring and Control")



# Input for PV name

pv_name = st.text_input("Enter PV Name", "TS01C-EA-GIRDR-01:TMON01")



if pv_name:

    # Get the PV object

    pv = epics.PV(pv_name)



    # Display PV information

    st.subheader("PV Information")

    st.write(f"PV Name: {pv.pvname}")

    st.write(f"Current Value: {pv.get()}")

    st.write(f"Units: {pv.units}")

    st.write(f"Precision: {pv.precision}")



    # Real-time monitoring

    st.subheader("Real-Time Monitoring")

    if st.button("Start Monitoring"):

        while True:

            value = pv.get()

            st.write(f"Current Value: {value}")

            st.line_chart([value])  # Simple visualization

            st.rerun()  # Refresh the app



    # Control PV value

    st.subheader("Control PV Value")

    new_value = st.number_input("Enter New Value", value=float(pv.get()))

    if st.button("Set Value"):

        pv.put(new_value)

        st.success(f"Value set to {new_value}")