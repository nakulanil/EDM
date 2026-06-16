(function() {
    function updateHint() {
        const examDateSelect = document.getElementById('id_exam_date');
        const hintField = document.querySelector('.field-needed_slots_hint p, .field-needed_slots_hint div');

        if (!examDateSelect || !hintField) return;

        const examDateId = examDateSelect.value;

        if (!examDateId) {
            hintField.textContent = "Select an exam date first";
            return;
        }

        // Fetch slots_count from our endpoint
        fetch(`/teachers/exam-slots-count/${examDateId}/`)
            .then(response => response.json())
            .then(data => {
                hintField.textContent = `This exam needs ${data.slots_count} total slots`;
            })
            .catch(() => {
                hintField.textContent = "Could not load hint";
            });
    }

    document.addEventListener('DOMContentLoaded', function() {
        const examDateSelect = document.getElementById('id_exam_date');
        if (examDateSelect) {
            examDateSelect.addEventListener('change', updateHint);
            updateHint(); // run on load too
        }
    });
})();