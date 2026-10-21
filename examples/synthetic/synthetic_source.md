# Synthetic lambda example

This document and all accompanying values were created from toy equations. They contain no vehicle, customer, employee, ECU calibration, or supplier documentation.

Six channels are sampled at 10 Hz for 60 seconds across nine operating points. Requested lambda is 1.0. Measured lambda is 1 + 0.005 * sin(t / 2). Only N20_RL50 has an artificial +0.12 offset from 20.0 through 24.0 seconds.

The demonstration flags abs(lambda_actual - lambda_target) > 0.05 for at least 0.5 seconds. This limit is invented for a software demo and is not a calibration recommendation, regulatory limit, or validation criterion for a real vehicle.
