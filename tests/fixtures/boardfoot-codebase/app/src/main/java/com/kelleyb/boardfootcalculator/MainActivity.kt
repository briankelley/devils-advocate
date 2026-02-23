package com.kelleyb.boardfootcalculator

import android.os.Bundle
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.button.MaterialButton
import com.google.android.material.textfield.TextInputEditText

class MainActivity : AppCompatActivity() {

    private lateinit var editPrice: TextInputEditText
    private lateinit var editLength: TextInputEditText
    private lateinit var editWidth: TextInputEditText
    private lateinit var editThickness: TextInputEditText
    private lateinit var textResult: TextView
    private lateinit var textTotal: TextView
    private lateinit var btnCalculate: MaterialButton
    private lateinit var btnClearAll: MaterialButton

    private var totalBoardFeet: Double = 0.0
    private var totalCost: Double = 0.0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        editPrice = findViewById(R.id.editPrice)
        editLength = findViewById(R.id.editLength)
        editWidth = findViewById(R.id.editWidth)
        editThickness = findViewById(R.id.editThickness)
        textResult = findViewById(R.id.textResult)
        textTotal = findViewById(R.id.textTotal)
        btnCalculate = findViewById(R.id.btnCalculate)
        btnClearAll = findViewById(R.id.btnClearAll)

        btnCalculate.setOnClickListener { calculate() }
        btnClearAll.setOnClickListener { clearAll() }
    }

    private fun calculate() {
        val price = editPrice.text.toString().toDoubleOrNull()
        if (price == null || price == 0.0) {
            Toast.makeText(this, R.string.toast_set_price, Toast.LENGTH_SHORT).show()
            return
        }

        val length = editLength.text.toString().toDoubleOrNull()
        val width = editWidth.text.toString().toDoubleOrNull()
        val thickness = editThickness.text.toString().toDoubleOrNull()

        if (length == null || width == null || thickness == null ||
            length == 0.0 || width == 0.0 || thickness == 0.0) {
            Toast.makeText(this, R.string.toast_enter_dimensions, Toast.LENGTH_SHORT).show()
            return
        }

        val boardFeet = Math.round((length * width * thickness) / 144.0 * 100.0) / 100.0
        val cost = Math.round(boardFeet * price * 100.0) / 100.0

        val lengthStr = formatDimension(length)
        val widthStr = formatDimension(width)
        val thicknessStr = formatDimension(thickness)

        textResult.text = getString(R.string.result_format, lengthStr, widthStr, thicknessStr, boardFeet, cost)

        totalBoardFeet += boardFeet
        totalCost += cost
        textTotal.text = getString(R.string.total_format, totalBoardFeet, totalCost)

        editLength.text?.clear()
        editWidth.text?.clear()
        editThickness.text?.clear()
        editLength.requestFocus()
    }

    private fun clearAll() {
        totalBoardFeet = 0.0
        totalCost = 0.0
        textResult.text = ""
        textTotal.text = getString(R.string.total_default)
        editLength.text?.clear()
        editWidth.text?.clear()
        editThickness.text?.clear()
        editLength.requestFocus()
    }

    private fun formatDimension(value: Double): String {
        return if (value == value.toLong().toDouble()) {
            value.toLong().toString()
        } else {
            value.toString()
        }
    }
}
